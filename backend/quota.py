"""Account-level indexed-source quota helpers.

Supabase owns identity; these helpers only read the verified user id and
account entitlements from Turso. Quota is based on the bytes of eligible files
that are currently indexed, not on temporary clone size or excluded files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from config import settings


class RepositoryQuotaExceededError(ValueError):
    """Raised when a repository would exceed the user's current plan quota."""


def _as_int(value: Any, fallback: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return fallback


def format_bytes(value: int) -> str:
    """Format bytes for concise user-facing account information."""
    value = _as_int(value)
    if value >= 1_000_000_000:
        amount, unit = value / 1_000_000_000, "GB"
    elif value >= 1_000_000:
        amount, unit = value / 1_000_000, "MB"
    elif value >= 1_000:
        amount, unit = value / 1_000, "KB"
    else:
        return f"{value} bytes"
    # Preserve the compact labels used in the UI while avoiding the previous
    # ``0 MB`` result for a small but non-empty repository.
    rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{rendered} {unit}"


async def get_account_usage(store, user_id: str) -> dict[str, Any]:
    """Return plan, quota, and current indexed bytes for one account."""
    entitlement = await store.fetch_one(
        "SELECT plan, quota_bytes, status, updated_at FROM account_entitlements WHERE user_id = ?",
        [user_id],
    )
    plan = str((entitlement or {}).get("plan") or "explorer")
    status = str((entitlement or {}).get("status") or "active")
    team_active = plan == "team" and status == "active"
    if team_active:
        # Standard Checkout is a one-time payment. Treat the verified payment
        # timestamp as the start of a 30-day entitlement so a stale payment
        # cannot grant the higher quota indefinitely.
        try:
            paid_at = datetime.fromisoformat(str(entitlement.get("updated_at")).replace("Z", "+00:00"))
            team_active = datetime.now(UTC) < paid_at + timedelta(days=max(1, settings.team_plan_duration_days))
        except (TypeError, ValueError):
            team_active = False
    if team_active:
        quota_bytes = min(
            _as_int((entitlement or {}).get("quota_bytes"), settings.team_codebase_bytes),
            settings.team_codebase_bytes,
        )
    else:
        if plan == "team" and status == "active":
            status = "expired"
        plan = "explorer"
        quota_bytes = settings.free_codebase_bytes
    usage = await store.fetch_one(
        "SELECT COALESCE(SUM(rf.byte_size), 0) AS used_bytes "
        "FROM repo_files rf JOIN repos r ON r.id = rf.repo_id WHERE r.user_id = ?",
        [user_id],
    )
    used_bytes = _as_int((usage or {}).get("used_bytes"))
    # ``repo_files`` was added after the first version of the indexer. Older
    # ready repositories can therefore have chunks but no manifest rows. Keep
    # those repositories visible in the account meter instead of reporting
    # zero until the user happens to re-index them. The fallback measures the
    # source payload already stored in chunks and is intentionally limited to
    # repositories that have no manifest, so current indexes are never counted
    # twice. New indexes always use the exact manifest byte size above.
    legacy_usage = await store.fetch_one(
        "SELECT COALESCE(SUM(length(CAST(c.content AS BLOB))), 0) AS used_bytes, "
        "COUNT(DISTINCT c.repo_id) AS legacy_repositories "
        "FROM chunks c JOIN repos r ON r.id = c.repo_id "
        "WHERE r.user_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM repo_files rf WHERE rf.repo_id = c.repo_id"
        ")",
        [user_id],
    )
    legacy_bytes = _as_int((legacy_usage or {}).get("used_bytes"))
    legacy_repositories = _as_int((legacy_usage or {}).get("legacy_repositories"))
    used_bytes += legacy_bytes
    return {
        "plan": plan,
        "status": status,
        "used_bytes": used_bytes,
        "quota_bytes": quota_bytes,
        "remaining_bytes": max(0, quota_bytes - used_bytes),
        "used_label": format_bytes(used_bytes),
        "quota_label": format_bytes(quota_bytes),
        "remaining_label": format_bytes(max(0, quota_bytes - used_bytes)),
        "legacy_repositories": legacy_repositories,
        "usage_estimated": legacy_repositories > 0,
    }


async def current_repository_bytes(store, repo_id: str) -> int:
    """Return the indexed bytes currently attributed to one repository."""
    current = await store.fetch_one(
        "SELECT COALESCE(SUM(byte_size), 0) AS used_bytes FROM repo_files WHERE repo_id = ?",
        [repo_id],
    )
    used = _as_int((current or {}).get("used_bytes"))
    if used == 0:
        # Legacy indexes predate the manifest; measure their stored chunks so
        # a refresh does not count the same repository twice.
        legacy = await store.fetch_one(
            "SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) AS used_bytes FROM chunks WHERE repo_id = ?",
            [repo_id],
        )
        used = _as_int((legacy or {}).get("used_bytes"))
    return used


def quota_exceeded_message(usage: dict[str, Any]) -> str:
    if usage.get("plan") == "team":
        return (
            f"Your Team workspace includes up to {format_bytes(usage['quota_bytes'])} of indexed source and it is full. "
            "Remove a repository or contact support for a higher limit."
        )
    return (
        f"Your Explorer workspace includes up to {format_bytes(usage['quota_bytes'])} of indexed source and it is full. "
        "Delete a repository or choose the Team plan for ₹300/month to index more."
    )


async def repository_byte_budget(store, user_id: str, repo_id: str | None) -> tuple[int, dict[str, Any]]:
    """Return the bytes this repository may index without exceeding the plan.

    The file selector ranks files by importance and stops at this budget, so
    a large repository on a nearly full plan indexes its most important code
    instead of failing outright. The repository's own previous index is
    excluded because a refresh replaces it.
    """
    usage = await get_account_usage(store, user_id)
    replacing = await current_repository_bytes(store, repo_id) if repo_id else 0
    committed_elsewhere = max(0, usage["used_bytes"] - replacing)
    return max(0, usage["quota_bytes"] - committed_elsewhere), usage


async def ensure_repository_usage_capacity(
    store,
    user_id: str,
    requested_bytes: int,
    replacing_repo_id: str | None = None,
) -> None:
    """Fail before publishing an index that would exceed the plan quota.

    During re-indexing the previous version of the same repository is removed
    from the calculation, so unchanged files do not count twice.
    """
    usage = await get_account_usage(store, user_id)
    current_repo_bytes = 0
    if replacing_repo_id:
        current = await store.fetch_one(
            "SELECT COALESCE(SUM(byte_size), 0) AS used_bytes FROM repo_files WHERE repo_id = ?",
            [replacing_repo_id],
        )
        current_repo_bytes = _as_int((current or {}).get("used_bytes"))
        # Apply the same compatibility fallback used by the account meter so
        # re-indexing a legacy repository does not count its old chunks twice
        # and get rejected by an otherwise available quota.
        if current_repo_bytes == 0:
            legacy_current = await store.fetch_one(
                "SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) AS used_bytes "
                "FROM chunks WHERE repo_id = ?",
                [replacing_repo_id],
            )
            current_repo_bytes = _as_int((legacy_current or {}).get("used_bytes"))
    committed_bytes = max(0, usage["used_bytes"] - current_repo_bytes)
    requested_bytes = _as_int(requested_bytes)
    projected_bytes = committed_bytes + requested_bytes
    if projected_bytes <= usage["quota_bytes"]:
        return
    upgrade_message = (
        f"Your Explorer workspace includes up to {format_bytes(usage['quota_bytes'])} of indexed source. "
        "This repository would exceed that limit. Choose the Team plan for ₹300/month to continue."
    )
    if usage["plan"] == "team":
        upgrade_message = (
            f"Your Team workspace includes up to {format_bytes(usage['quota_bytes'])} of indexed source. "
            "Remove a repository or contact support for a higher limit."
        )
    raise RepositoryQuotaExceededError(upgrade_message)
