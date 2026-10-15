import base64
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import Security, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import settings
from database import supabase, DatabaseConfigurationError

security = HTTPBearer()

@dataclass
class AuthenticatedUser:
    id: str
    access_token: str
    raw_user: object


# Status polling authenticates every few seconds, and each validation is a
# Supabase round trip. Supabase stays the only authority: the cache merely
# reuses its recent answer for a short window, so a token revoked upstream is
# still honoured for at most settings.auth_cache_seconds.
#
# The bound keeps a flood of distinct (even valid) tokens from growing the
# process without limit on the 512 MB instance.
_AUTH_CACHE_MAX_ENTRIES = 4096
# The entry bound alone does not bound bytes. Each entry keeps the Supabase
# user object, whose user_metadata the signed-in user can set freely, and a
# single account can mint any number of distinct valid tokens by refreshing.
# Measured: ~10 KB of metadata (still inside uvicorn's 16 KiB request-head
# limit) costs ~250 KB per entry, so one account filling 4096 slots would pin
# ~1 GiB and also evict every other user's session. Capping slots per account
# bounds what one account can pin (and evict) to a few MiB. Legitimate users
# need one slot per device plus a brief overlap when a token is refreshed.
_AUTH_CACHE_MAX_ENTRIES_PER_USER = 8
# An opaque or exp-less token gives no hint about when it stops being valid,
# so it gets a shorter ceiling than a JWT whose expiry is known.
_AUTH_CACHE_NO_EXP_SECONDS = 60.0
# Supabase copies user_metadata, which the signed-in user can set freely, into
# every access token it signs. json's C parser spends C stack on every nesting
# level and raises RecursionError only after roughly 10,000 levels, so on a
# worker-thread stack below about 2 MiB (musl's default is 128 KiB; build flags
# and RLIMIT_STACK can shrink it too) the stack overflows first and the whole
# process dies instead of raising. A payload with no more brackets than this
# cannot nest deeper than this, so it goes straight to the parser; any other
# payload has its nested containers collapsed first (_collapse_nested_json).
_JWT_MAX_NESTING = 32
_JSON_OPENER_FOR = {"]": "[", "}": "{"}

# sha256(token) -> (expires_at on the monotonic clock, verified user).
# OrderedDict preserves insertion order, which is the eviction order.
_auth_cache: OrderedDict[str, tuple[float, AuthenticatedUser]] = OrderedDict()
# user id -> that user's cache keys, oldest first (a dict used as an ordered
# set). It lets the per-user cap find a user's oldest entry without scanning
# the whole cache, and must change in step with _auth_cache (_forget_locked).
_auth_cache_keys_by_user: dict[str, dict[str, None]] = {}
# FastAPI runs synchronous dependencies in a threadpool, so concurrent
# requests touch the cache from several threads at once.
_auth_cache_lock = threading.Lock()


def clear_auth_cache() -> None:
    """Forget every cached session (used by tests to isolate cases)."""
    with _auth_cache_lock:
        _auth_cache.clear()
        _auth_cache_keys_by_user.clear()


def _token_cache_key(token: str) -> str:
    # Keys are hashes so the key space never holds a replayable bearer token.
    # surrogatepass makes encoding total, so an odd token can never turn an
    # authentication failure into a server error before Supabase sees it.
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _collapse_nested_json(text: str) -> str | None:
    """Return JSON ``text`` with every array/object nested inside a top-level
    value replaced by ``null``, or None when its brackets cannot be valid JSON.

    exp is a top-level member, so the collapsed text yields the same exp as
    the original while handing json's parser at most one level of nesting.
    Skipping exp instead would fall back to the 60 second ceiling and could
    keep a nearly expired token cached past its own exp. ``null`` (not ``0``)
    keeps a container-valued exp from turning into a numeric one.

    One linear pass with an explicit stack and no recursion, so it is safe on
    any thread stack. Brackets inside string literals are skipped.
    """
    pieces: list[str] = []
    openers: list[str] = []
    # Start of the top-level text not yet copied into pieces.
    resume = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "[" or char == "{":
            openers.append(char)
            if len(openers) == 2:
                pieces.append(text[resume:index])
        elif char == "]" or char == "}":
            # A stray or mismatched closer is invalid JSON wherever it sits,
            # exactly as json.loads would have reported.
            if not openers or openers.pop() != _JSON_OPENER_FOR[char]:
                return None
            if len(openers) == 1:
                pieces.append("null")
                resume = index + 1
    # An unterminated string or container is invalid JSON as well.
    if in_string or openers:
        return None
    pieces.append(text[resume:])
    return "".join(pieces)


def _jwt_expiry(token: str) -> float | None:
    """Return the JWT ``exp`` claim, or None when the token carries none.

    The signature is deliberately NOT verified. The value only shortens how
    long a Supabase-verified session is reused; it is never used to decide
    whether a request is authorized.
    """
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    segment = parts[1]
    try:
        # JWTs use unpadded base64url; restore the padding the decoder expects.
        payload = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        # Decode exactly as json.loads(bytes) does internally, so the bracket
        # scan inspects the very text the parser will see.
        text = payload.decode(json.detect_encoding(payload), "surrogatepass")
        # Depth can never exceed the number of opening brackets, so the usual
        # small claim set skips the per-character collapse loop.
        if text.count("[") + text.count("{") > _JWT_MAX_NESTING:
            # This only ever runs on a token Supabase has just accepted, i.e.
            # on Supabase-signed (valid) JSON. For valid JSON the collapsed
            # text has exactly the original top-level members.
            collapsed = _collapse_nested_json(text)
            if collapsed is None:
                return None
            text = collapsed
        claims = json.loads(text)
        if not isinstance(claims, dict):
            return None
        exp = claims.get("exp")
        # bool is an int subclass, but true/false is not a timestamp.
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        expiry = float(exp)
    # ValueError covers bad base64, undecodable bytes and malformed JSON, and
    # an enormous integer raises OverflowError; both simply mean "no usable
    # expiry hint". The collapse above keeps deep payloads away from the
    # parser; RecursionError stays as a second line of defence.
    except (ValueError, RecursionError, OverflowError):
        return None
    if not math.isfinite(expiry):
        return None
    return expiry


def _cache_ttl_seconds(token: str) -> float:
    """Seconds a freshly verified token may be reused; 0 means do not cache."""
    ceiling = settings.auth_cache_seconds
    if ceiling <= 0:
        return 0.0
    expiry = _jwt_expiry(token)
    if expiry is None:
        return float(min(ceiling, _AUTH_CACHE_NO_EXP_SECONDS))
    # exp is a wall-clock timestamp, so wall-clock time is used only for this
    # comparison. The cache deadline itself is monotonic so a later clock
    # adjustment cannot stretch a cached session.
    remaining = expiry - time.time()
    if remaining <= 0:
        return 0.0
    return float(min(ceiling, remaining))


def _cache_deadline(token: str) -> float | None:
    """Monotonic instant a freshly verified token stops being reusable, or None."""
    # The monotonic base is read BEFORE _cache_ttl_seconds reads the wall
    # clock. Reading it later (for example after waiting on the cache lock)
    # would add that wait on top of the time left until exp, letting the entry
    # outlive the token it stands for.
    now = time.monotonic()
    ttl = _cache_ttl_seconds(token)
    if ttl <= 0:
        return None
    return now + ttl


def _forget_locked(key: str) -> None:
    """Remove ``key`` from the cache and the per-user index; the caller must
    hold _auth_cache_lock. Every removal also unindexes (here, or directly via
    _unindex_locked) so the index never keeps keys, and so memory, for
    entries that are already gone."""
    entry = _auth_cache.pop(key, None)
    if entry is not None:
        _unindex_locked(entry[1].id, key)


def _unindex_locked(user_id: str, key: str) -> None:
    """Drop ``key`` from ``user_id``'s index; the caller must hold the lock."""
    user_keys = _auth_cache_keys_by_user.get(user_id)
    if user_keys is None:
        return
    user_keys.pop(key, None)
    if not user_keys:
        del _auth_cache_keys_by_user[user_id]


def _cache_lookup(key: str) -> AuthenticatedUser | None:
    with _auth_cache_lock:
        entry = _auth_cache.get(key)
        if entry is None:
            return None
        expires_at, user = entry
        if expires_at > time.monotonic():
            return user
        # Dropping on read stops an expired session lingering until eviction.
        _forget_locked(key)
        return None


def _drop_expired_locked(now: float) -> None:
    """Remove expired entries; the caller must hold _auth_cache_lock."""
    expired = [key for key, (expires_at, _user) in _auth_cache.items() if expires_at <= now]
    for key in expired:
        _forget_locked(key)


def _cache_store(key: str, user: AuthenticatedUser, expires_at: float) -> None:
    if _AUTH_CACHE_MAX_ENTRIES <= 0 or _AUTH_CACHE_MAX_ENTRIES_PER_USER <= 0:
        return
    with _auth_cache_lock:
        now = time.monotonic()
        # A very short-lived token can expire while this thread waits for the
        # lock. Storing it would be dead on arrival and, on a full cache, would
        # evict a live session to make room for nothing.
        if expires_at <= now:
            return
        # Concurrent misses for one token each store a result. Removing the
        # old entry first means it is not counted against the bound and the
        # key moves to the newest position, keeping eviction insertion-ordered.
        _forget_locked(key)
        # A user at their cap gives up their own oldest slot first, so one
        # account minting tokens recycles its own slots instead of pushing
        # other users' sessions out of a full cache.
        user_keys = _auth_cache_keys_by_user.get(user.id)
        while user_keys and len(user_keys) >= _AUTH_CACHE_MAX_ENTRIES_PER_USER:
            oldest = next(iter(user_keys))
            # Unindexing by user id (not via the entry) shrinks user_keys on
            # every pass, so this loop cannot spin while holding the lock.
            _auth_cache.pop(oldest, None)
            _unindex_locked(user.id, oldest)
        if len(_auth_cache) >= _AUTH_CACHE_MAX_ENTRIES:
            # Expired entries are free to drop; only evict live sessions
            # (oldest first) when that does not make room.
            _drop_expired_locked(now)
        while len(_auth_cache) >= _AUTH_CACHE_MAX_ENTRIES:
            _forget_locked(next(iter(_auth_cache)))
        _auth_cache[key] = (expires_at, user)
        _auth_cache_keys_by_user.setdefault(user.id, {})[key] = None


def _verify_with_supabase(token: str) -> AuthenticatedUser:
    try:
        user_res = supabase.auth.get_user(token)
        if not user_res or not user_res.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid access token or expired session"
            )
        return AuthenticatedUser(
            id=user_res.user.id,
            access_token=token,
            raw_user=user_res.user
        )
    except HTTPException:
        raise
    except DatabaseConfigurationError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed. Sign in again and retry."
        )


def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security)) -> AuthenticatedUser:
    token = credentials.credentials
    if settings.auth_cache_seconds <= 0:
        return _verify_with_supabase(token)

    key = _token_cache_key(token)
    cached = _cache_lookup(key)
    if cached is not None:
        return cached

    # Only a successful verification reaches the store: failures raise above,
    # so an outage or a rejected token is always re-checked on the next call.
    user = _verify_with_supabase(token)
    expires_at = _cache_deadline(token)
    if expires_at is not None:
        _cache_store(key, user, expires_at)
    return user
