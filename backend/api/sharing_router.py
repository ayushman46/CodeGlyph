"""Share a repository with teammates through invite links.

The owner (the user who indexed the repository) creates invite links with a
role and an expiry, sees and revokes the active ones, changes members'
roles and removes members. Anyone signed in who opens a link sees what it
grants and joins; the repository then appears in their workspace under an
alias that is unique among their own repositories. Members can leave.

Links are long random tokens; only their SHA-256 is stored, so a link is
shown once, when it is created. Access checks for every repository route
live in ``api.repository_state`` (``fetch_accessible_repository``).
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import get_current_user
from api.repository_state import (
    raise_storage_error,
    resolve_repository_access,
    taken_repository_names,
    unique_alias,
)
from database import assert_turso_schema, get_turso_store
from ingest.cloner import RepositoryValidationError, parse_github_repository

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sharing"])

INVITE_PATH = "/invite/"
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LABEL_MAX = 120
# Active (unexpired, unrevoked) links per repository.
MAX_ACTIVE_INVITES = 20
_INVALID_LINK = "This invite link isn't valid. Check that you copied the whole link, or ask for a new one."


class InviteRequest(BaseModel):
    role: Literal["editor", "viewer"] = "viewer"
    expires_in_days: int = Field(default=7, ge=1, le=30)


class RoleRequest(BaseModel):
    role: Literal["editor", "viewer"]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def account_label(user: Any) -> str:
    """How other people see this account: email, else name, else its id."""
    raw = getattr(user, "raw_user", None)
    if isinstance(raw, dict):
        email, metadata = raw.get("email"), raw.get("user_metadata")
    else:
        email, metadata = getattr(raw, "email", None), getattr(raw, "user_metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    name = metadata.get("full_name") or metadata.get("name")
    for candidate in (email, name, getattr(user, "id", "")):
        text = " ".join(str(candidate or "").split())
        if text:
            return text[:_LABEL_MAX]
    return "A teammate"


def _full_name(github_url: Any) -> str:
    try:
        owner, name = parse_github_repository(str(github_url or ""))
    except (RepositoryValidationError, ValueError):
        return ""
    return f"{owner}/{name}"


def _invite_state(invite: dict[str, Any], now: datetime) -> str:
    if invite.get("revoked_at"):
        return "revoked"
    expires = _parse_time(invite.get("expires_at"))
    if expires is None or expires <= now:
        return "expired"
    return "active"


def _public_invite(invite: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "id": invite["id"],
        "role": invite["role"],
        "created_at": invite["created_at"],
        "expires_at": invite["expires_at"],
        # The last characters of the token, so the owner can tell links apart.
        "token_hint": invite["token_hint"],
        "accepted_count": int(invite.get("accepted_count") or 0),
        "state": _invite_state(invite, now),
    }


def _public_member(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": member["user_id"],
        "label": member.get("member_label") or member["user_id"],
        "role": member["role"],
        "joined_at": member["joined_at"],
        "updated_at": member.get("updated_at"),
    }


def _storage_errors(action: str):
    def handle(error: Exception):
        raise_storage_error(error, action, logger)
    return handle


async def _store():
    await assert_turso_schema()
    return get_turso_store()


# ------------------------------------------------------------------ owner


@router.get("/repos/{repo_name}/sharing")
async def get_sharing(repo_name: str, current_user=Depends(get_current_user)):
    """Members and active invite links of a repository you own."""
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, repo_name, permission="manage")
        members = await store.fetch_all(
            "SELECT user_id, role, member_label, joined_at, updated_at FROM repo_members "
            "WHERE repo_id = ? AND removed_at IS NULL ORDER BY joined_at",
            [access["id"]],
        )
        now = _now()
        invites = await store.fetch_all(
            "SELECT id, role, created_at, expires_at, token_hint, accepted_count, revoked_at FROM repo_invites "
            "WHERE repo_id = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY created_at DESC",
            [access["id"], _iso(now)],
        )
        return {
            "repository": repo_name,
            "full_name": _full_name(access["github_url"]),
            "members": [_public_member(member) for member in members],
            "invites": [_public_invite(invite, now) for invite in invites],
        }
    except Exception as error:
        _storage_errors("load who this repository is shared with")(error)


@router.post("/repos/{repo_name}/invites")
async def create_invite(repo_name: str, payload: InviteRequest, current_user=Depends(get_current_user)):
    """Create an invite link. The link (with its token) is returned only now."""
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, repo_name, permission="manage")
        now = _now()
        active = await store.fetch_one(
            "SELECT COUNT(*) AS count FROM repo_invites WHERE repo_id = ? AND revoked_at IS NULL AND expires_at > ?",
            [access["id"], _iso(now)],
        )
        if int((active or {}).get("count") or 0) >= MAX_ACTIVE_INVITES:
            raise HTTPException(
                status_code=409,
                detail=f"This repository already has {MAX_ACTIVE_INVITES} active invite links. Revoke one you no longer need first.",
            )
        token = secrets.token_urlsafe(32)
        invite = {
            "id": str(uuid4()),
            "role": payload.role,
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(days=payload.expires_in_days)),
            "token_hint": token[-4:],
            "accepted_count": 0,
        }
        await store.execute(
            "INSERT INTO repo_invites (id, repo_id, created_by, token_hash, token_hint, role, owner_label, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [invite["id"], access["id"], current_user.id, hash_token(token), invite["token_hint"], payload.role,
             account_label(current_user), invite["created_at"], invite["expires_at"]],
        )
        return {"invite": _public_invite(invite, now), "token": token, "path": f"{INVITE_PATH}{token}"}
    except Exception as error:
        _storage_errors("create the invite link")(error)


@router.delete("/repos/{repo_name}/invites/{invite_id}")
async def revoke_invite(repo_name: str, invite_id: str, current_user=Depends(get_current_user)):
    """Stop an invite link from working. People who already joined stay members."""
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, repo_name, permission="manage")
        result = await store.execute(
            "UPDATE repo_invites SET revoked_at = ? WHERE id = ? AND repo_id = ? AND revoked_at IS NULL",
            [_iso(_now()), invite_id, access["id"]],
        )
        if not result.rows_affected:
            existing = await store.fetch_one("SELECT 1 AS ok FROM repo_invites WHERE id = ? AND repo_id = ?", [invite_id, access["id"]])
            if not existing:
                raise HTTPException(status_code=404, detail="That invite link was not found. Refresh the list.")
        return {"revoked": True, "id": invite_id}
    except Exception as error:
        _storage_errors("revoke the invite link")(error)


async def _active_member(store, repo_id: str, member_id: str) -> dict[str, Any]:
    member = await store.fetch_one(
        "SELECT user_id, role, member_label, joined_at, updated_at FROM repo_members "
        "WHERE repo_id = ? AND user_id = ? AND removed_at IS NULL",
        [repo_id, member_id],
    )
    if not member:
        raise HTTPException(status_code=404, detail="That person is not a member of this repository. Refresh the list.")
    return member


@router.patch("/repos/{repo_name}/members/{member_id}")
async def change_member_role(repo_name: str, member_id: str, payload: RoleRequest, current_user=Depends(get_current_user)):
    """Make a member an editor or a viewer. Takes effect on their next request."""
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, repo_name, permission="manage")
        member = await _active_member(store, access["id"], member_id)
        updated_at = _iso(_now())
        await store.execute(
            "UPDATE repo_members SET role = ?, updated_at = ? WHERE repo_id = ? AND user_id = ? AND removed_at IS NULL",
            [payload.role, updated_at, access["id"], member_id],
        )
        return {"member": _public_member({**member, "role": payload.role, "updated_at": updated_at})}
    except Exception as error:
        _storage_errors("change the member's role")(error)


@router.delete("/repos/{repo_name}/members/{member_id}")
async def remove_member(repo_name: str, member_id: str, current_user=Depends(get_current_user)):
    """Remove a member. Their next request for the repository gets 404."""
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, repo_name, permission="manage")
        await _active_member(store, access["id"], member_id)
        now = _iso(_now())
        # The row stays (removed) so the former member is told the repository
        # is no longer shared, rather than that it never existed.
        await store.execute(
            "UPDATE repo_members SET removed_at = ?, updated_at = ? WHERE repo_id = ? AND user_id = ? AND removed_at IS NULL",
            [now, now, access["id"], member_id],
        )
        return {"removed": True, "user_id": member_id}
    except Exception as error:
        _storage_errors("remove the member")(error)


# ----------------------------------------------------------------- member


@router.delete("/repos/{repo_name}/membership")
async def leave_repository(repo_name: str, current_user=Depends(get_current_user)):
    """Leave a repository someone shared with you."""
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, repo_name)
        if access["role"] == "owner":
            raise HTTPException(
                status_code=409,
                detail="You own this repository, so you can't leave it. Delete it instead if you no longer need it.",
            )
        await store.execute(
            "DELETE FROM repo_members WHERE repo_id = ? AND user_id = ? AND removed_at IS NULL", [access["id"], current_user.id],
        )
        return {"left": True, "repo_name": repo_name}
    except Exception as error:
        _storage_errors("leave the repository")(error)


# ----------------------------------------------------------------- invite


async def _load_invite(store, token: str) -> dict[str, Any]:
    """The invite for ``token`` (with its repository), or a clear refusal."""
    if not _TOKEN_PATTERN.fullmatch(token or ""):
        raise HTTPException(status_code=404, detail=_INVALID_LINK, headers={"X-Error-Code": "invite_invalid"})
    invite = await store.fetch_one(
        "SELECT i.id, i.repo_id, i.role, i.owner_label, i.created_at, i.expires_at, i.revoked_at, "
        "r.user_id AS owner_id, r.repo_name, r.github_url FROM repo_invites i JOIN repos r ON r.id = i.repo_id "
        "WHERE i.token_hash = ?",
        [hash_token(token)],
    )
    if not invite:
        raise HTTPException(status_code=404, detail=_INVALID_LINK, headers={"X-Error-Code": "invite_invalid"})
    owner = invite.get("owner_label") or "the person who shared it"
    state = _invite_state(invite, _now())
    if state == "revoked":
        raise HTTPException(
            status_code=410, detail=f"This invite link was revoked. Ask {owner} for a new link.",
            headers={"X-Error-Code": "invite_revoked"},
        )
    if state == "expired":
        raise HTTPException(
            status_code=410,
            detail=f"This invite link has expired. Ask {owner} for a new link.",
            headers={"X-Error-Code": "invite_expired"},
        )
    return invite


async def _membership(store, repo_id: str, user_id: str) -> dict[str, Any] | None:
    return await store.fetch_one(
        "SELECT role, alias, removed_at FROM repo_members WHERE repo_id = ? AND user_id = ?", [repo_id, user_id],
    )


def _removed_since_invite(membership: dict[str, Any] | None, invite: dict[str, Any]) -> bool:
    """A link made before the owner removed someone cannot bring them back."""
    if not membership or not membership.get("removed_at"):
        return False
    removed = _parse_time(membership["removed_at"])
    created = _parse_time(invite.get("created_at"))
    return removed is None or created is None or removed >= created


@router.get("/invites/{token}")
async def preview_invite(token: str, current_user=Depends(get_current_user)):
    """What an invite link grants, and whether you already have access."""
    try:
        store = await _store()
        invite = await _load_invite(store, token)
        membership = await _membership(store, invite["repo_id"], current_user.id)
        if invite["owner_id"] == current_user.id:
            status, repo_name, role = "owner", invite["repo_name"], "owner"
        elif membership and not membership.get("removed_at"):
            status, repo_name, role = "member", membership["alias"], membership["role"]
        elif _removed_since_invite(membership, invite):
            status, repo_name, role = "removed", None, None
        else:
            status, repo_name, role = "valid", None, None
        return {
            "status": status,
            "repository": invite["repo_name"],
            "full_name": _full_name(invite["github_url"]),
            "owner_label": invite.get("owner_label") or None,
            "role": invite["role"],
            "expires_at": invite["expires_at"],
            # How you know the repository now, when you already have access.
            "repo_name": repo_name,
            "access_role": role,
        }
    except Exception as error:
        _storage_errors("open the invite link")(error)


@router.post("/invites/{token}/accept")
async def accept_invite(token: str, current_user=Depends(get_current_user)):
    """Join the repository an invite link shares, under a name of your own."""
    try:
        store = await _store()
        invite = await _load_invite(store, token)
        owner = invite.get("owner_label") or "the owner"
        if invite["owner_id"] == current_user.id:
            raise HTTPException(status_code=409, detail="You own this repository already. Open it from your workspace.")
        membership = await _membership(store, invite["repo_id"], current_user.id)
        if membership and not membership.get("removed_at"):
            # Joining again never changes a role the owner has set.
            return {"repo_name": membership["alias"], "role": membership["role"], "joined": False}
        if _removed_since_invite(membership, invite):
            raise HTTPException(
                status_code=403,
                detail=f"{owner} removed your access to this repository after this link was created. Ask them for a new link.",
                headers={"X-Error-Code": "share_removed"},
            )
        now = _iso(_now())
        values = [invite["role"], account_label(current_user), invite.get("owner_label") or "", invite["id"], now, now]
        for _attempt in range(3):
            alias = unique_alias(str(invite["repo_name"]), await taken_repository_names(store, current_user.id))
            try:
                if membership:
                    # Rejoining after being removed (with a newer link) or leaving.
                    await store.execute(
                        "UPDATE repo_members SET role = ?, member_label = ?, owner_label = ?, invite_id = ?, joined_at = ?, "
                        "updated_at = ?, alias = ?, removed_at = NULL WHERE repo_id = ? AND user_id = ? AND removed_at IS NOT NULL",
                        [*values, alias, invite["repo_id"], current_user.id],
                    )
                else:
                    await store.execute(
                        "INSERT INTO repo_members (role, member_label, owner_label, invite_id, joined_at, updated_at, alias, repo_id, user_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(repo_id, user_id) DO NOTHING",
                        [*values, alias, invite["repo_id"], current_user.id],
                    )
            except Exception as error:
                # Another request took the alias in the meantime: pick again.
                if "unique" in str(error).lower():
                    continue
                raise
            break
        else:
            raise HTTPException(status_code=409, detail="Joining collided with another request. Open the link again.")
        joined = await _membership(store, invite["repo_id"], current_user.id)
        if not joined or joined.get("removed_at"):
            raise HTTPException(status_code=409, detail="Joining collided with another request. Open the link again.")
        await store.execute("UPDATE repo_invites SET accepted_count = accepted_count + 1 WHERE id = ?", [invite["id"]])
        return {"repo_name": joined["alias"], "role": joined["role"], "joined": True}
    except Exception as error:
        _storage_errors("join the repository")(error)
