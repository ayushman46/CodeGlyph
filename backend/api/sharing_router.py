"""Share a repository with teammates through invite links, and share answers.

The owner (the user who indexed the repository) creates invite links with a
role and an expiry, sees and revokes the active ones, changes members'
roles and removes members. Anyone signed in who opens a link sees what it
grants and joins; the repository then appears in their workspace under an
alias that is unique among their own repositories. Members can leave.

Anyone who can open a repository can also share one of their answers as a
read-only link: a snapshot of the question, the answer and its citations.

Links are long random tokens; only their SHA-256 is stored, so a link is
shown once, when it is created. Access checks for every repository route
live in ``api.repository_state`` (``fetch_accessible_repository``).
"""

from __future__ import annotations

import hashlib
import json
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
    repository_access_by_id,
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


# Invite and answer links carry their token in the URL path, both the API's
# and the web app's. Request logs show the start of the token's stored hash
# instead (enough to find its row), so logs never hold a working link.
_TOKEN_IN_PATH = re.compile(r"^(/api/invites/|/api/shared-answers/|/invite/|/shared/answer/)([^/?#]+)")


def redact_link_tokens(path: str) -> str:
    """``path`` with an invite or answer link's token replaced by a fingerprint."""
    return _TOKEN_IN_PATH.sub(lambda match: f"{match[1]}[token:{hash_token(match[2])[:8]}]", str(path or ""), count=1)


class RedactLinkTokens(logging.Filter):
    """Redacts link tokens in uvicorn's access log ("%s - "%s %s HTTP/%s" %d")."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_link_tokens(args[2]), *args[3:])
        return True


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
            "SELECT user_id, role, member_label, joined_at, updated_at, invite_id FROM repo_members "
            "WHERE repo_id = ? AND removed_at IS NULL ORDER BY joined_at",
            [access["id"]],
        )
        now = _now()
        invites = await store.fetch_all(
            "SELECT id, role, created_at, expires_at, token_hint, accepted_count, revoked_at FROM repo_invites "
            "WHERE repo_id = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY created_at DESC",
            [access["id"], _iso(now)],
        )
        # Who joined through each link (and is still a member): a link can be
        # used by anyone signed in, so the owner sees who actually used it.
        joined_by: dict[str, list[dict[str, Any]]] = {}
        for member in members:
            if member.get("invite_id"):
                joined_by.setdefault(member["invite_id"], []).append(
                    {"user_id": member["user_id"], "label": member.get("member_label") or member["user_id"]})
        return {
            "repository": repo_name,
            "full_name": _full_name(access["github_url"]),
            "members": [_public_member(member) for member in members],
            "invites": [{**_public_invite(invite, now), "joined_by": joined_by.get(invite["id"], [])} for invite in invites],
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
        # role_changed_at outlasts leaving: a link made before this change
        # cannot override the role when the member leaves and joins again.
        await store.execute(
            "UPDATE repo_members SET role = ?, updated_at = ?, role_changed_at = ? "
            "WHERE repo_id = ? AND user_id = ? AND removed_at IS NULL",
            [payload.role, updated_at, updated_at, access["id"], member_id],
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
        # is no longer shared, rather than that it never existed, and so links
        # made before now cannot bring them back.
        await store.execute(
            "UPDATE repo_members SET removed_at = ?, owner_removed_at = ?, updated_at = ? "
            "WHERE repo_id = ? AND user_id = ? AND removed_at IS NULL",
            [now, now, now, access["id"], member_id],
        )
        return {"removed": True, "user_id": member_id}
    except Exception as error:
        _storage_errors("remove the member")(error)


# ----------------------------------------------------------------- member


@router.delete("/repos/{repo_name}/membership")
async def leave_repository(repo_name: str, current_user=Depends(get_current_user)):
    """Leave a repository someone shared with you.

    An invite link that still works brings you back, with the role the owner
    last set for you if they changed it after that link was made.
    """
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, repo_name)
        if access["role"] == "owner":
            raise HTTPException(
                status_code=409,
                detail="You own this repository, so you can't leave it. Delete it instead if you no longer need it.",
            )
        # The row stays (left) and keeps the role and the owner's earlier
        # decisions, so joining again cannot undo them (see _join_role).
        now = _iso(_now())
        await store.execute(
            "UPDATE repo_members SET removed_at = ?, left_at = ?, updated_at = ? "
            "WHERE repo_id = ? AND user_id = ? AND removed_at IS NULL",
            [now, now, now, access["id"], current_user.id],
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
        "SELECT role, alias, removed_at, owner_removed_at, role_changed_at FROM repo_members "
        "WHERE repo_id = ? AND user_id = ?",
        [repo_id, user_id],
    )


def _decided_since_invite(decided_at: Any, invite: dict[str, Any]) -> bool:
    """Whether the owner made a decision (at ``decided_at``) after the link was created."""
    if not decided_at:
        return False
    decided = _parse_time(decided_at)
    created = _parse_time(invite.get("created_at"))
    return decided is None or created is None or decided >= created


def _removed_since_invite(membership: dict[str, Any] | None, invite: dict[str, Any]) -> bool:
    """A link made before the owner removed someone cannot bring them back.

    This holds even after they came back with a newer link and then left.
    """
    if not membership or not membership.get("removed_at"):
        return False
    return _decided_since_invite(membership.get("owner_removed_at"), invite)


def _join_role(membership: dict[str, Any] | None, invite: dict[str, Any]) -> str:
    """The role joining with ``invite`` gives: the link's, unless the person
    was a member before and the owner set their role after the link was made.
    So leaving and joining again never undoes the owner's role change."""
    if membership and _decided_since_invite(membership.get("role_changed_at"), invite):
        return str(membership["role"])
    return str(invite["role"])


@router.get("/invites/{token}")
async def preview_invite(token: str, current_user=Depends(get_current_user)):
    """What an invite link grants, and whether you already have access."""
    try:
        store = await _store()
        invite = await _load_invite(store, token)
        membership = await _membership(store, invite["repo_id"], current_user.id)
        join_role = invite["role"]
        if invite["owner_id"] == current_user.id:
            status, repo_name, role = "owner", invite["repo_name"], "owner"
        elif membership and not membership.get("removed_at"):
            status, repo_name, role = "member", membership["alias"], membership["role"]
        elif _removed_since_invite(membership, invite):
            status, repo_name, role = "removed", None, None
        else:
            status, repo_name, role = "valid", None, None
            join_role = _join_role(membership, invite)
        return {
            "status": status,
            "repository": invite["repo_name"],
            "full_name": _full_name(invite["github_url"]),
            "owner_label": invite.get("owner_label") or None,
            # The role joining gives you. It differs from the link's own role
            # (role_kept) when you were a member before and the owner changed
            # your role after this link was made.
            "role": join_role,
            "role_kept": join_role != invite["role"],
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
        values = [_join_role(membership, invite), account_label(current_user), invite.get("owner_label") or "", invite["id"], now, now]
        for _attempt in range(3):
            alias = unique_alias(str(invite["repo_name"]), await taken_repository_names(store, current_user.id))
            try:
                if membership:
                    # Rejoining after leaving, or with a link created after the
                    # owner removed them. owner_removed_at and role_changed_at stay.
                    await store.execute(
                        "UPDATE repo_members SET role = ?, member_label = ?, owner_label = ?, invite_id = ?, joined_at = ?, "
                        "updated_at = ?, alias = ?, removed_at = NULL, left_at = NULL "
                        "WHERE repo_id = ? AND user_id = ? AND removed_at IS NOT NULL",
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


# ----------------------------------------------------------- shared answers


ANSWER_PATH = "/shared/answer/"
# Active answer links per person.
MAX_ACTIVE_ANSWER_SHARES = 200
_ANSWER_UNAVAILABLE = (
    "This shared answer isn't available. The link may be incomplete, or the answer is no longer shared."
)
_COMMIT_IN_URL = re.compile(r"/blob/([0-9a-f]{7,40})/")
_CITATION_FIELDS = ("file_path", "start_line", "end_line", "github_url", "language", "cited_in_answer")


class AnswerShareRequest(BaseModel):
    repo_name: str = Field(min_length=1, max_length=200)
    # The stored message id (an answer loaded from history) and/or the answer
    # text (an answer streamed in this session, whose stored id the page does
    # not know). Either way the snapshot is copied from the stored message.
    message_id: str = Field(default="", max_length=100)
    answer: str = Field(default="", max_length=200_000)


def _answer_snapshot(question: str, message: dict[str, Any], github_url: Any, shared_by: str) -> dict[str, Any]:
    citations = message.get("citations") if isinstance(message.get("citations"), list) else []
    kept = [
        {field: citation.get(field) for field in _CITATION_FIELDS if field in citation}
        for citation in citations if isinstance(citation, dict) and citation.get("file_path")
    ][:30]
    commits = (_COMMIT_IN_URL.search(str(item.get("github_url") or "")) for item in kept)
    return {
        "question": question,
        "answer": str(message.get("content") or ""),
        "citations": kept,
        "repository": _full_name(github_url),
        "commit_sha": next((match.group(1) for match in commits if match), None),
        "answered_at": message.get("created_at"),
        "shared_by": shared_by,
    }


@router.post("/answers/share")
async def share_answer(payload: AnswerShareRequest, current_user=Depends(get_current_user)):
    """Create a read-only link to one of your answers (shown once)."""
    try:
        store = await _store()
        access = await resolve_repository_access(store, current_user.id, payload.repo_name)
        select = (
            "SELECT id, content, citations, created_at FROM chat_messages "
            "WHERE repo_id = ? AND user_id = ? AND role = 'assistant' "
        )
        message = None
        if payload.message_id:
            message = await store.fetch_one(select + "AND id = ?", [access["id"], current_user.id, payload.message_id])
        if not message and payload.answer.strip():
            message = await store.fetch_one(
                select + "AND content = ? ORDER BY created_at DESC LIMIT 1", [access["id"], current_user.id, payload.answer],
            )
        if not message:
            raise HTTPException(
                status_code=404,
                detail="That answer isn't saved in your conversation, so it can't be shared. Ask again, then share the new answer.",
            )
        active = await store.fetch_one(
            "SELECT COUNT(*) AS count FROM answer_shares WHERE user_id = ? AND revoked_at IS NULL", [current_user.id],
        )
        if int((active or {}).get("count") or 0) >= MAX_ACTIVE_ANSWER_SHARES:
            raise HTTPException(
                status_code=409,
                detail=f"You have {MAX_ACTIVE_ANSWER_SHARES} shared answers. Stop sharing some on your account page first.",
            )
        question = await store.fetch_one(
            "SELECT content FROM chat_messages WHERE repo_id = ? AND user_id = ? AND role = 'user' AND created_at <= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            [access["id"], current_user.id, message["created_at"]],
        )
        snapshot = _answer_snapshot(
            str((question or {}).get("content") or ""), message, access["github_url"], account_label(current_user),
        )
        token = secrets.token_urlsafe(32)
        share_id, created_at = str(uuid4()), _iso(_now())
        await store.execute(
            "INSERT INTO answer_shares (id, user_id, repo_id, message_id, token_hash, snapshot_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [share_id, current_user.id, access["id"], message["id"], hash_token(token),
             json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False), created_at],
        )
        return {"id": share_id, "token": token, "path": f"{ANSWER_PATH}{token}", "created_at": created_at}
    except Exception as error:
        _storage_errors("share the answer")(error)


@router.get("/shared-answers/{token}")
async def open_shared_answer(token: str, current_user=Depends(get_current_user)):
    """A shared answer, for any signed-in user with the link."""
    unavailable = HTTPException(status_code=404, detail=_ANSWER_UNAVAILABLE, headers={"X-Error-Code": "answer_unavailable"})
    try:
        if not _TOKEN_PATTERN.fullmatch(token or ""):
            raise unavailable
        store = await _store()
        row = await store.fetch_one(
            "SELECT id, user_id, repo_id, snapshot_json, created_at FROM answer_shares WHERE token_hash = ? AND revoked_at IS NULL",
            [hash_token(token)],
        )
        # A link stops working once its author can no longer open the
        # repository (they left or were removed, or it was deleted).
        if not row or not await repository_access_by_id(store, str(row["user_id"]), str(row["repo_id"])):
            raise unavailable
        snapshot = json.loads(row["snapshot_json"])
        return {**snapshot, "id": row["id"], "shared_at": row["created_at"], "yours": row["user_id"] == current_user.id}
    except Exception as error:
        _storage_errors("open the shared answer")(error)


@router.get("/answers/shares")
async def list_answer_shares(current_user=Depends(get_current_user)):
    """Your active shared answers, newest first."""
    try:
        store = await _store()
        rows = await store.fetch_all(
            "SELECT id, snapshot_json, created_at FROM answer_shares WHERE user_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC LIMIT 100",
            [current_user.id],
        )
        shares = []
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            shares.append({
                "id": row["id"], "created_at": row["created_at"], "repository": snapshot.get("repository"),
                "question": str(snapshot.get("question") or "")[:200],
            })
        return {"shares": shares}
    except Exception as error:
        _storage_errors("load your shared answers")(error)


@router.delete("/answers/shares/{share_id}")
async def revoke_answer_share(share_id: str, current_user=Depends(get_current_user)):
    """Stop sharing an answer: its link returns 404 from now on."""
    try:
        store = await _store()
        result = await store.execute(
            "UPDATE answer_shares SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            [_iso(_now()), share_id, current_user.id],
        )
        if not result.rows_affected:
            mine = await store.fetch_one("SELECT 1 AS ok FROM answer_shares WHERE id = ? AND user_id = ?", [share_id, current_user.id])
            if not mine:
                raise HTTPException(status_code=404, detail="That shared answer was not found. Refresh the list.")
        return {"revoked": True, "id": share_id}
    except Exception as error:
        _storage_errors("stop sharing the answer")(error)
