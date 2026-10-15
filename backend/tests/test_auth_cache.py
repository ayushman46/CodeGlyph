import base64
import gc
import hashlib
import inspect
import json
import random
import subprocess
import sys
import threading
import time as real_time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

import api.auth as auth
from database import DatabaseConfigurationError

WALL_START = 1_700_000_000.0
MONO_START = 1_000.0


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_jwt(claims, *, raw_payload: bytes | None = None) -> str:
    """Build an unsigned-looking JWT; the cache never verifies the signature."""
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(raw_payload if raw_payload is not None else json.dumps(claims).encode())
    return f"{header}.{payload}.signature"


def bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def user_response(user_id: str):
    return SimpleNamespace(user=SimpleNamespace(id=user_id))


def cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class FakeClock:
    """Stands in for the time module inside api.auth so tests control both clocks."""

    def __init__(self, wall: float = WALL_START, mono: float = MONO_START):
        self.wall = wall
        self.mono = mono

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


class ContendedLock:
    """Wraps the real cache lock and advances the fake clocks on every
    acquisition, as if the thread had queued behind other requests."""

    def __init__(self, lock, clock: FakeClock, wait_seconds: float):
        self._lock = lock
        self._clock = clock
        self._wait_seconds = wait_seconds

    def __enter__(self):
        self._lock.acquire()
        self._clock.advance(self._wait_seconds)
        return self

    def __exit__(self, *exc_info):
        self._lock.release()
        return False


class AuthCacheTestCase(unittest.TestCase):
    def setUp(self):
        auth.clear_auth_cache()
        self.addCleanup(auth.clear_auth_cache)
        self.clock = FakeClock()
        # Patch settings in the auth module so a local .env (for example
        # AUTH_CACHE_SECONDS=0) cannot change what these tests observe.
        self.settings = SimpleNamespace(auth_cache_seconds=120)
        self.supabase = MagicMock()
        self.get_user = self.supabase.auth.get_user
        self.get_user.return_value = user_response("user-1")
        for target, value in (
            ("api.auth.time", self.clock),
            ("api.auth.settings", self.settings),
            ("api.auth.supabase", self.supabase),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def jwt(self, seconds_until_exp: float, sub: str = "user-1") -> str:
        return make_jwt({"sub": sub, "exp": self.clock.wall + seconds_until_exp})

    def cached_ttl(self, token: str) -> float:
        expires_at, _user = auth._auth_cache[cache_key(token)]
        return expires_at - self.clock.mono

    def assert_index_consistent(self) -> None:
        """The per-user index must list exactly the cached keys, per user, in
        insertion order; a stale key or empty group would leak memory."""
        expected: dict[str, list[str]] = {}
        for key, (_expires_at, user) in auth._auth_cache.items():
            expected.setdefault(user.id, []).append(key)
        actual = {user_id: list(keys) for user_id, keys in auth._auth_cache_keys_by_user.items()}
        self.assertEqual(actual, expected)


class CacheHitTests(AuthCacheTestCase):
    def test_second_call_with_same_token_does_not_call_supabase(self):
        token = self.jwt(3600)

        first = auth.get_current_user(bearer(token))
        second = auth.get_current_user(bearer(token))

        self.get_user.assert_called_once_with(token)
        self.assertEqual(first.id, "user-1")
        self.assertEqual(second.id, "user-1")
        self.assertEqual(second.access_token, token)
        self.assertIsInstance(second, auth.AuthenticatedUser)

    def test_different_token_calls_supabase(self):
        token_a = self.jwt(3600, sub="user-a")
        token_b = self.jwt(3600, sub="user-b")
        self.get_user.side_effect = lambda token: user_response("user-a" if token == token_a else "user-b")

        user_a = auth.get_current_user(bearer(token_a))
        user_b = auth.get_current_user(bearer(token_b))

        self.assertEqual(self.get_user.call_count, 2)
        self.assertEqual([call.args[0] for call in self.get_user.call_args_list], [token_a, token_b])
        self.assertEqual((user_a.id, user_b.id), ("user-a", "user-b"))
        self.assertEqual(user_b.access_token, token_b)

        # Both are now cached independently.
        self.assertEqual(auth.get_current_user(bearer(token_a)).id, "user-a")
        self.assertEqual(auth.get_current_user(bearer(token_b)).id, "user-b")
        self.assertEqual(self.get_user.call_count, 2)

    def test_cache_key_is_sha256_hex_of_token_not_raw_token(self):
        token = self.jwt(3600)

        auth.get_current_user(bearer(token))

        self.assertEqual(list(auth._auth_cache.keys()), [cache_key(token)])
        self.assertNotIn(token, auth._auth_cache)
        key = next(iter(auth._auth_cache))
        self.assertEqual(len(key), 64)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in key))

    def test_clear_auth_cache_forces_revalidation(self):
        token = self.jwt(3600)
        auth.get_current_user(bearer(token))

        auth.clear_auth_cache()

        self.assertEqual(len(auth._auth_cache), 0)
        self.assertEqual(auth._auth_cache_keys_by_user, {})
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)
        self.assert_index_consistent()


class TtlTests(AuthCacheTestCase):
    def test_expired_jwt_is_never_cached(self):
        token = self.jwt(-1)

        for _ in range(3):
            self.assertEqual(auth.get_current_user(bearer(token)).id, "user-1")

        self.assertEqual(self.get_user.call_count, 3)
        self.assertEqual(len(auth._auth_cache), 0)

    def test_jwt_expiring_exactly_now_is_not_cached(self):
        token = self.jwt(0)

        auth.get_current_user(bearer(token))
        auth.get_current_user(bearer(token))

        self.assertEqual(self.get_user.call_count, 2)
        self.assertEqual(len(auth._auth_cache), 0)

    def test_token_expiring_in_five_seconds_is_cached_at_most_five_seconds(self):
        token = self.jwt(5)

        auth.get_current_user(bearer(token))
        self.assertLessEqual(self.cached_ttl(token), 5.0)
        self.assertEqual(self.cached_ttl(token), 5.0)

        self.clock.advance(4.5)
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 1)

        # At exp the cached entry must already be gone.
        self.clock.advance(0.5)
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)
        # The JWT is expired at this moment, so the fresh answer is not cached.
        self.assertEqual(len(auth._auth_cache), 0)

    def test_fractional_exp_bounds_ttl(self):
        token = self.jwt(7.5)

        auth.get_current_user(bearer(token))

        self.assertEqual(self.cached_ttl(token), 7.5)

    def test_ttl_never_exceeds_configured_seconds(self):
        token = self.jwt(3600)

        auth.get_current_user(bearer(token))
        self.assertEqual(self.cached_ttl(token), 120.0)

        self.clock.advance(119.5)
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 1)

        self.clock.advance(0.5)
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)
        # Still 3480s from exp, so it is cached again for the full ceiling.
        self.assertEqual(self.cached_ttl(token), 120.0)

    def test_exp_closer_than_ceiling_wins_and_ceiling_closer_than_exp_wins(self):
        self.settings.auth_cache_seconds = 10
        far = self.jwt(30, sub="far")
        near = self.jwt(4, sub="near")

        auth.get_current_user(bearer(far))
        auth.get_current_user(bearer(near))

        self.assertEqual(self.cached_ttl(far), 10.0)
        self.assertEqual(self.cached_ttl(near), 4.0)

    def test_token_without_usable_exp_is_cached_for_at_most_sixty_seconds(self):
        cases = {
            "opaque token": "opaque-session-token",
            "two segments": "abc.def",
            "four segments": "a.b.c.d",
            "empty payload": "header..signature",
            "invalid base64": "header.%%%%.signature",
            "impossible base64 length": "header.abcde.signature",
            "non-JSON payload": make_jwt(None, raw_payload=b"not json"),
            "non-UTF8 payload": make_jwt(None, raw_payload=b"\xff\xfe\xfd"),
            "JSON array payload": make_jwt(["exp", 1]),
            "missing exp": make_jwt({"sub": "user-1"}),
            "string exp": make_jwt({"exp": str(int(WALL_START + 3600))}),
            "boolean exp": make_jwt({"exp": True}),
            "null exp": make_jwt({"exp": None}),
            "infinite exp": make_jwt({"exp": float("inf")}),
            "NaN exp": make_jwt({"exp": float("nan")}),
            "overflowing exp": make_jwt({"exp": 10**400}),
            "deeply nested payload": make_jwt(None, raw_payload=b"[" * 100_000),
        }
        for label, token in cases.items():
            with self.subTest(label):
                auth.clear_auth_cache()
                self.get_user.reset_mock()

                auth.get_current_user(bearer(token))
                self.assertEqual(self.cached_ttl(token), 60.0)

                self.clock.advance(59.5)
                auth.get_current_user(bearer(token))
                self.assertEqual(self.get_user.call_count, 1)

                self.clock.advance(0.5)
                auth.get_current_user(bearer(token))
                self.assertEqual(self.get_user.call_count, 2)

    def test_token_without_exp_uses_smaller_configured_ceiling(self):
        self.settings.auth_cache_seconds = 30
        token = "opaque-session-token"

        auth.get_current_user(bearer(token))

        self.assertEqual(self.cached_ttl(token), 30.0)

    def test_padded_payload_is_still_decoded(self):
        # This claim set's length is not a multiple of 3, so the encoding ends in "==".
        claims = {"sub": "u1", "exp": WALL_START + 9}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode("ascii")
        self.assertTrue(payload.endswith("="), "fixture must exercise explicit padding")
        token = f"header.{payload}.signature"

        auth.get_current_user(bearer(token))

        self.assertEqual(self.cached_ttl(token), 9.0)

    def test_cache_expiry_follows_monotonic_clock_not_wall_clock(self):
        token = self.jwt(3600)
        auth.get_current_user(bearer(token))

        # A wall-clock jump must not expire (or extend) a cached session.
        self.clock.wall += 1_000
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 1)

        self.clock.wall -= 5_000
        self.clock.mono += 120
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)

    def test_waiting_for_the_lock_cannot_extend_entry_past_exp(self):
        token = self.jwt(5)
        # Both fake clocks move together, so exp maps to this monotonic instant.
        exp_on_monotonic_clock = self.clock.mono + 5

        contended = ContendedLock(auth._auth_cache_lock, self.clock, 1.0)
        with patch.object(auth, "_auth_cache_lock", contended):
            auth.get_current_user(bearer(token))

        expires_at, _user = auth._auth_cache[cache_key(token)]
        self.assertLessEqual(expires_at, exp_on_monotonic_clock)

        # Once exp is reached the cached answer must no longer be served.
        self.clock.advance(exp_on_monotonic_clock - self.clock.mono)
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)

    def test_expired_entry_is_removed_on_lookup(self):
        token = self.jwt(5)
        auth.get_current_user(bearer(token))
        self.get_user.side_effect = RuntimeError("supabase down")

        self.clock.advance(6)
        with self.assertRaises(HTTPException):
            auth.get_current_user(bearer(token))

        self.assertNotIn(cache_key(token), auth._auth_cache)
        self.assert_index_consistent()
        self.assertEqual(auth._auth_cache_keys_by_user, {})


class FailureTests(AuthCacheTestCase):
    def test_exception_then_success_is_not_cached(self):
        token = self.jwt(3600)
        self.get_user.side_effect = [RuntimeError("network"), user_response("user-1")]

        with self.assertRaises(HTTPException) as raised:
            auth.get_current_user(bearer(token))
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Authentication failed. Sign in again and retry.")
        self.assertEqual(len(auth._auth_cache), 0)

        self.assertEqual(auth.get_current_user(bearer(token)).id, "user-1")
        self.assertEqual(self.get_user.call_count, 2)

        # The success is cached; no third Supabase call.
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)

    def test_invalid_session_is_not_cached(self):
        token = self.jwt(3600)
        for rejected in (None, SimpleNamespace(user=None)):
            with self.subTest(rejected=rejected):
                auth.clear_auth_cache()
                self.get_user.reset_mock()
                self.get_user.side_effect = [rejected, user_response("user-1")]

                with self.assertRaises(HTTPException) as raised:
                    auth.get_current_user(bearer(token))
                self.assertEqual(raised.exception.status_code, 401)
                self.assertEqual(raised.exception.detail, "Invalid access token or expired session")
                self.assertEqual(len(auth._auth_cache), 0)

                self.assertEqual(auth.get_current_user(bearer(token)).id, "user-1")
                self.assertEqual(self.get_user.call_count, 2)

    def test_configuration_error_returns_503_and_is_not_cached(self):
        token = self.jwt(3600)
        self.get_user.side_effect = [DatabaseConfigurationError("Supabase is not configured"), user_response("user-1")]

        with self.assertRaises(HTTPException) as raised:
            auth.get_current_user(bearer(token))
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Supabase is not configured")
        self.assertEqual(len(auth._auth_cache), 0)

        self.assertEqual(auth.get_current_user(bearer(token)).id, "user-1")
        self.assertEqual(self.get_user.call_count, 2)

    def test_http_exception_from_supabase_is_propagated_and_not_cached(self):
        token = self.jwt(3600)
        upstream = HTTPException(status_code=401, detail="custom")
        self.get_user.side_effect = upstream

        with self.assertRaises(HTTPException) as raised:
            auth.get_current_user(bearer(token))

        self.assertIs(raised.exception, upstream)
        self.assertEqual(len(auth._auth_cache), 0)

    def test_errors_match_uncached_behaviour_when_cache_disabled(self):
        self.settings.auth_cache_seconds = 0
        token = self.jwt(3600)
        self.get_user.side_effect = [
            None,
            DatabaseConfigurationError("not configured"),
            RuntimeError("boom"),
        ]
        expected = [
            (401, "Invalid access token or expired session"),
            (503, "not configured"),
            (401, "Authentication failed. Sign in again and retry."),
        ]
        for status_code, detail in expected:
            with self.subTest(status_code=status_code, detail=detail):
                with self.assertRaises(HTTPException) as raised:
                    auth.get_current_user(bearer(token))
                self.assertEqual((raised.exception.status_code, raised.exception.detail), (status_code, detail))


class DisabledCacheTests(AuthCacheTestCase):
    def test_zero_or_negative_cache_seconds_disables_cache(self):
        for seconds in (0, -5):
            with self.subTest(auth_cache_seconds=seconds):
                auth.clear_auth_cache()
                self.get_user.reset_mock()
                self.settings.auth_cache_seconds = seconds
                token = self.jwt(3600)

                for _ in range(3):
                    self.assertEqual(auth.get_current_user(bearer(token)).id, "user-1")

                self.assertEqual(self.get_user.call_count, 3)
                self.assertEqual(len(auth._auth_cache), 0)

    def test_disabling_cache_ignores_entries_cached_earlier(self):
        token = self.jwt(3600)
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 1)

        self.settings.auth_cache_seconds = 0
        auth.get_current_user(bearer(token))

        self.assertEqual(self.get_user.call_count, 2)


class EvictionTests(AuthCacheTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(auth, "_AUTH_CACHE_MAX_ENTRIES", 3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_size_never_exceeds_max_and_oldest_inserted_is_evicted(self):
        tokens = [self.jwt(3600, sub=f"user-{index}") for index in range(10)]

        for token in tokens:
            auth.get_current_user(bearer(token))
            self.assertLessEqual(len(auth._auth_cache), 3)

        self.assertEqual(list(auth._auth_cache.keys()), [cache_key(token) for token in tokens[-3:]])

        # The newest tokens are still cached; an evicted one is re-verified.
        auth.get_current_user(bearer(tokens[-1]))
        self.assertEqual(self.get_user.call_count, 10)
        auth.get_current_user(bearer(tokens[0]))
        self.assertEqual(self.get_user.call_count, 11)
        self.assertLessEqual(len(auth._auth_cache), 3)
        self.assert_index_consistent()

    def test_expired_entries_are_dropped_before_live_ones(self):
        live_b = self.jwt(3600, sub="b")
        short_lived = self.jwt(10, sub="short")
        live_c = self.jwt(3600, sub="c")
        # The expired entry sits in the middle, so plain oldest-first eviction
        # would wrongly remove live_b and keep the dead session.
        for token in (live_b, short_lived, live_c):
            auth.get_current_user(bearer(token))

        # short_lived is now expired but has not been looked up since.
        self.clock.advance(20)
        newcomer = self.jwt(3600, sub="d")
        auth.get_current_user(bearer(newcomer))

        self.assertEqual(
            list(auth._auth_cache.keys()),
            [cache_key(live_b), cache_key(live_c), cache_key(newcomer)],
        )
        # live_b, the oldest entry, survived because the expired one made room.
        auth.get_current_user(bearer(live_b))
        self.assertEqual(self.get_user.call_count, 4)
        self.assert_index_consistent()

    def test_all_expired_entries_are_dropped_when_full(self):
        short_tokens = [self.jwt(10, sub=f"short-{index}") for index in range(3)]
        for token in short_tokens:
            auth.get_current_user(bearer(token))

        self.clock.advance(20)
        newcomer = self.jwt(3600, sub="new")
        auth.get_current_user(bearer(newcomer))

        self.assertEqual(list(auth._auth_cache.keys()), [cache_key(newcomer)])
        self.assert_index_consistent()

    def test_restoring_an_existing_key_does_not_evict_another_entry(self):
        # Concurrent misses for one token each store a result; the second
        # store must replace the entry rather than push out a live session.
        user_a = auth.AuthenticatedUser(id="a", access_token="token-a", raw_user=None)
        user_b = auth.AuthenticatedUser(id="b", access_token="token-b", raw_user=None)
        user_c = auth.AuthenticatedUser(id="c", access_token="token-c", raw_user=None)
        expires_at = self.clock.mono + 60
        for user in (user_a, user_b, user_c):
            auth._cache_store(cache_key(user.access_token), user, expires_at)

        auth._cache_store(cache_key("token-a"), user_a, expires_at)

        self.assertEqual(
            list(auth._auth_cache.keys()),
            [cache_key("token-b"), cache_key("token-c"), cache_key("token-a")],
        )
        self.assert_index_consistent()

    def test_storing_an_already_expired_deadline_is_a_no_op(self):
        live = [self.jwt(3600, sub=f"live-{index}") for index in range(3)]
        for token in live:
            auth.get_current_user(bearer(token))
        late = auth.AuthenticatedUser(id="late", access_token="token-late", raw_user=None)

        for expires_at in (self.clock.mono, self.clock.mono - 1):
            with self.subTest(expires_at=expires_at):
                auth._cache_store(cache_key("token-late"), late, expires_at)
                self.assertEqual(list(auth._auth_cache.keys()), [cache_key(token) for token in live])

    def test_token_expiring_while_waiting_for_lock_is_not_stored_and_evicts_nothing(self):
        live = [self.jwt(3600, sub=f"live-{index}") for index in range(3)]
        for token in live:
            auth.get_current_user(bearer(token))
        short_lived = self.jwt(1, sub="short")

        # The lookup wait leaves 0.5s until exp; the store wait uses it up.
        contended = ContendedLock(auth._auth_cache_lock, self.clock, 0.5)
        with patch.object(auth, "_auth_cache_lock", contended):
            self.assertEqual(auth.get_current_user(bearer(short_lived)).id, "user-1")

        self.assertEqual(list(auth._auth_cache.keys()), [cache_key(token) for token in live])
        self.assertEqual(self.get_user.call_count, 4)


class DefaultBoundTests(AuthCacheTestCase):
    def test_default_bound_is_4096(self):
        self.assertEqual(auth._AUTH_CACHE_MAX_ENTRIES, 4096)

    def test_default_bound_keeps_at_most_4096_entries(self):
        tokens = [f"opaque-token-{index}" for index in range(4096 + 4)]
        # One account per token, so only the global bound (not the per-user
        # cap) decides what is evicted.
        self.get_user.side_effect = lambda token: user_response(f"account-{token}")

        for token in tokens:
            auth.get_current_user(bearer(token))

        self.assertEqual(len(auth._auth_cache), 4096)
        self.assert_index_consistent()
        # The four oldest were evicted, in insertion order.
        self.assertEqual(next(iter(auth._auth_cache)), cache_key(tokens[4]))
        for evicted in tokens[:4]:
            self.assertNotIn(cache_key(evicted), auth._auth_cache)
        self.assertIn(cache_key(tokens[-1]), auth._auth_cache)


class PerUserCapTests(AuthCacheTestCase):
    """One account can mint any number of distinct valid tokens (each refresh
    yields a new one), so the global bound alone would let it fill the cache."""

    def tokens_for(self, count: int, prefix: str = "session") -> list[str]:
        return [self.jwt(3600, sub=f"{prefix}-{index}") for index in range(count)]

    def test_default_per_user_cap_is_8(self):
        self.assertEqual(auth._AUTH_CACHE_MAX_ENTRIES_PER_USER, 8)

    def test_one_account_keeps_only_its_newest_tokens(self):
        cap = auth._AUTH_CACHE_MAX_ENTRIES_PER_USER
        # The unverified sub claims all differ; only Supabase's verified user
        # id groups the tokens, so forged claims cannot dodge the cap.
        tokens = self.tokens_for(cap + 12)

        for token in tokens:
            auth.get_current_user(bearer(token))
            self.assertLessEqual(len(auth._auth_cache), cap)

        self.assertEqual(list(auth._auth_cache), [cache_key(token) for token in tokens[-cap:]])
        self.assert_index_consistent()
        # The newest tokens are served from the cache; an evicted one is re-verified.
        auth.get_current_user(bearer(tokens[-1]))
        self.assertEqual(self.get_user.call_count, len(tokens))
        auth.get_current_user(bearer(tokens[0]))
        self.assertEqual(self.get_user.call_count, len(tokens) + 1)
        self.assertEqual(len(auth._auth_cache), cap)

    def test_flooding_account_cannot_evict_other_users_sessions(self):
        cap = auth._AUTH_CACHE_MAX_ENTRIES_PER_USER
        victims = [self.jwt(3600, sub=f"victim-{index}") for index in range(4)]
        attacker_tokens = self.tokens_for(50, prefix="attacker")
        owners = {token: f"victim-{index}" for index, token in enumerate(victims)}
        owners.update({token: "attacker" for token in attacker_tokens})
        self.get_user.side_effect = lambda token: user_response(owners[token])
        # Exactly full once the attacker reaches its cap, so any attacker token
        # that took a global slot instead of its own would evict a victim.
        patcher = patch.object(auth, "_AUTH_CACHE_MAX_ENTRIES", len(victims) + cap)
        patcher.start()
        self.addCleanup(patcher.stop)

        for token in victims + attacker_tokens:
            auth.get_current_user(bearer(token))

        calls_before = self.get_user.call_count
        for index, token in enumerate(victims):
            self.assertEqual(auth.get_current_user(bearer(token)).id, f"victim-{index}")
        self.assertEqual(self.get_user.call_count, calls_before, "a victim session was evicted")
        self.assertEqual(len(auth._auth_cache_keys_by_user["attacker"]), cap)
        self.assertEqual(len(auth._auth_cache), len(victims) + cap)
        self.assert_index_consistent()

    def test_restoring_a_token_moves_it_to_the_newest_slot_of_its_user(self):
        cap = auth._AUTH_CACHE_MAX_ENTRIES_PER_USER
        tokens = self.tokens_for(cap)
        for token in tokens:
            auth.get_current_user(bearer(token))
        first_user = auth._auth_cache[cache_key(tokens[0])][1]

        # A concurrent miss re-stores tokens[0]; it is now that user's newest.
        auth._cache_store(cache_key(tokens[0]), first_user, self.clock.mono + 60)
        newcomer = self.jwt(3600, sub="newcomer")
        auth.get_current_user(bearer(newcomer))

        self.assertIn(cache_key(tokens[0]), auth._auth_cache)
        self.assertNotIn(cache_key(tokens[1]), auth._auth_cache)
        self.assertEqual(list(auth._auth_cache)[-2:], [cache_key(tokens[0]), cache_key(newcomer)])
        self.assert_index_consistent()

    def test_cap_of_one_keeps_only_the_latest_token(self):
        with patch.object(auth, "_AUTH_CACHE_MAX_ENTRIES_PER_USER", 1):
            tokens = self.tokens_for(3)
            for token in tokens:
                auth.get_current_user(bearer(token))

        self.assertEqual(list(auth._auth_cache), [cache_key(tokens[-1])])
        self.assert_index_consistent()

    def test_non_positive_cap_stores_nothing_and_cannot_hang(self):
        for cap in (0, -1):
            with self.subTest(cap=cap), patch.object(auth, "_AUTH_CACHE_MAX_ENTRIES_PER_USER", cap):
                auth.clear_auth_cache()
                self.get_user.reset_mock()
                token = self.jwt(3600)

                for _ in range(2):
                    self.assertEqual(auth.get_current_user(bearer(token)).id, "user-1")

                self.assertEqual(self.get_user.call_count, 2)
                self.assertEqual(len(auth._auth_cache), 0)
                self.assertEqual(auth._auth_cache_keys_by_user, {})

    def test_index_and_bounds_hold_across_random_operations(self):
        # Mixes every removal path: lookup of an expired entry, the expired
        # sweep on a full cache, global oldest-first eviction, per-user
        # eviction, re-storing a present key, and clearing.
        rng = random.Random(4242)
        max_entries, per_user = 7, 3
        users = [f"user-{index}" for index in range(5)]
        owners: dict[str, str] = {}
        tokens: list[str] = []
        self.get_user.side_effect = lambda token: user_response(owners[token])
        with patch.object(auth, "_AUTH_CACHE_MAX_ENTRIES", max_entries), patch.object(
            auth, "_AUTH_CACHE_MAX_ENTRIES_PER_USER", per_user
        ):
            for step in range(4000):
                roll = rng.random()
                if roll < 0.15:
                    self.clock.advance(rng.choice([0.5, 3, 20, 130]))
                elif roll < 0.17:
                    auth.clear_auth_cache()
                elif roll < 0.25 and tokens:
                    token = rng.choice(tokens)
                    user = auth.AuthenticatedUser(id=owners[token], access_token=token, raw_user=None)
                    auth._cache_store(cache_key(token), user, self.clock.mono + rng.choice([1, 30, 90]))
                else:
                    if tokens and rng.random() < 0.5:
                        token = rng.choice(tokens)
                    else:
                        token = rng.choice([self.jwt(rng.choice([-1, 2, 10, 60, 3600]), sub=f"s{step}"), f"opaque-{step}"])
                        owners[token] = rng.choice(users)
                        tokens.append(token)
                    self.assertEqual(auth.get_current_user(bearer(token)).id, owners[token])

                self.assertLessEqual(len(auth._auth_cache), max_entries)
                for keys in auth._auth_cache_keys_by_user.values():
                    self.assertLessEqual(len(keys), per_user)
                self.assert_index_consistent()


class RetainedMemoryTests(AuthCacheTestCase):
    @staticmethod
    def heavy_user_response(_token):
        # user_metadata is set by the signed-in user. ~3000 empty objects is
        # ~11 KB of JSON (it fits in a 16 KiB request head) yet a few hundred
        # KB as Python objects.
        return SimpleNamespace(user=SimpleNamespace(id="attacker", user_metadata=[{} for _ in range(3000)]))

    def test_one_account_minting_tokens_cannot_pin_unbounded_memory(self):
        cap = auth._AUTH_CACHE_MAX_ENTRIES_PER_USER
        tokens = [self.jwt(3600, sub=f"session-{index}") for index in range(100)]
        self.get_user.side_effect = self.heavy_user_response
        gc.collect()
        tracemalloc.start()
        try:
            sample = self.heavy_user_response(None)
            one_user_bytes = tracemalloc.get_traced_memory()[0]
            del sample
            gc.collect()
            baseline = tracemalloc.get_traced_memory()[0]
            for token in tokens:
                auth.get_current_user(bearer(token))
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0] - baseline
        finally:
            tracemalloc.stop()

        self.assertEqual(len(auth._auth_cache), cap)
        # Without the per-user cap all 100 user objects stay pinned (~100x).
        self.assertLess(retained, (cap + 4) * one_user_bytes)


class ConcurrencyTests(AuthCacheTestCase):
    def run_threads(self, target, count: int = 8) -> list[BaseException]:
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def guarded(index: int) -> None:
            try:
                target(index)
            except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
                with errors_lock:
                    errors.append(error)

        # Daemon threads: if a regression deadlocks the cache lock, the join
        # timeout reports a failure instead of the interpreter hanging at exit
        # while it waits for non-daemon threads.
        threads = [threading.Thread(target=guarded, args=(index,), daemon=True) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads), "worker thread hung")
        return errors

    def test_same_token_from_eight_threads(self):
        token = self.jwt(3600)
        calls = []
        calls_lock = threading.Lock()

        def slow_get_user(received_token):
            # Widen the window so several threads miss the cache together.
            real_time.sleep(0.01)
            with calls_lock:
                calls.append(received_token)
            return user_response("user-1")

        self.get_user.side_effect = slow_get_user
        barrier = threading.Barrier(8)
        results = []
        results_lock = threading.Lock()

        def worker(_index: int) -> None:
            barrier.wait(timeout=5)
            for _ in range(25):
                user = auth.get_current_user(bearer(token))
                with results_lock:
                    results.append((user.id, user.access_token))

        errors = self.run_threads(worker)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8 * 25)
        self.assertEqual(set(results), {("user-1", token)})
        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 8)
        self.assertEqual(set(calls), {token})
        self.assertEqual(list(auth._auth_cache.keys()), [cache_key(token)])

    def test_distinct_tokens_from_eight_threads_respect_bound(self):
        patcher = patch.object(auth, "_AUTH_CACHE_MAX_ENTRIES", 5)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.get_user.side_effect = lambda token: user_response(token.rsplit(".", 1)[-1])
        barrier = threading.Barrier(8)
        mismatches = []

        def worker(index: int) -> None:
            barrier.wait(timeout=5)
            for item in range(50):
                token = f"opaque.thread{index}.item{item}"
                user = auth.get_current_user(bearer(token))
                if user.id != f"item{item}" or user.access_token != token:
                    mismatches.append(token)
                if len(auth._auth_cache) > 5:
                    mismatches.append(f"oversized:{len(auth._auth_cache)}")

        errors = self.run_threads(worker)

        self.assertEqual(errors, [])
        self.assertEqual(mismatches, [])
        self.assertLessEqual(len(auth._auth_cache), 5)
        self.assert_index_consistent()

    def test_threads_sharing_accounts_respect_global_and_per_user_bounds(self):
        max_entries, per_user = 10, 4
        for target, value in (("_AUTH_CACHE_MAX_ENTRIES", max_entries), ("_AUTH_CACHE_MAX_ENTRIES_PER_USER", per_user)):
            patcher = patch.object(auth, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Four accounts x four slots exceeds the global bound, so per-user
        # eviction, global eviction and same-key re-stores all race.
        self.get_user.side_effect = lambda token: user_response(f"account-{int(token.rsplit('-', 1)[-1]) % 4}")
        barrier = threading.Barrier(8)
        mismatches = []

        def worker(index: int) -> None:
            barrier.wait(timeout=5)
            for item in range(60):
                # Threads overlap on half the tokens to provoke duplicate stores.
                token = f"opaque-t{index if item % 2 else 'shared'}-{item}"
                user = auth.get_current_user(bearer(token))
                if user.id != f"account-{item % 4}" or user.access_token != token:
                    mismatches.append(token)
                if len(auth._auth_cache) > max_entries:
                    mismatches.append(f"oversized:{len(auth._auth_cache)}")

        errors = self.run_threads(worker)

        self.assertEqual(errors, [])
        self.assertEqual(mismatches, [])
        self.assertLessEqual(len(auth._auth_cache), max_entries)
        for keys in auth._auth_cache_keys_by_user.values():
            self.assertLessEqual(len(keys), per_user)
        self.assert_index_consistent()


def claims_with(extra_json: str, exp: float) -> bytes:
    """Raw claim-set bytes with a top-level exp followed by ``extra_json`` members."""
    return ('{"sub": "user-1", "exp": %s, %s}' % (json.dumps(exp), extra_json)).encode()


def nested_arrays(depth: int) -> str:
    return "[" * depth + "]" * depth


def json_depth(text: str) -> int:
    """Deepest array/object nesting in JSON ``text``, measured without recursion."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def top_level(value):
    """What the collapse must preserve: top-level members, containers as None."""
    def flat(item):
        return None if isinstance(item, (dict, list)) else item

    if isinstance(value, dict):
        return {key: flat(item) for key, item in value.items()}
    if isinstance(value, list):
        return [flat(item) for item in value]
    return value


def random_json(rng, depth: int):
    """A random JSON value nesting up to ``depth`` levels, with hostile strings."""
    strings = ["", "plain", "[{", "]}", 'quote"inside', "back\\slash", "\\", 'a\\"[', "é☃", "exp"]
    if depth <= 0 or rng.random() < 0.25:
        return rng.choice([None, True, False, rng.randint(-10**6, 10**6), rng.random() * 1e9, rng.choice(strings)])
    if rng.random() < 0.5:
        return [random_json(rng, depth - 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(strings) + str(index): random_json(rng, depth - 1) for index in range(rng.randint(0, 3))}


class CollapseNestedJsonTests(unittest.TestCase):
    """The linear pass that keeps hostile nesting away from json's C parser
    without losing the top-level exp."""

    def test_containers_below_the_top_level_become_null(self):
        text = '{"exp": 9, "m": %s, "o": {"a": [1, {"b": 2}]}, "s": "x"}' % nested_arrays(5000)

        collapsed = auth._collapse_nested_json(text)

        self.assertEqual(collapsed, '{"exp": 9, "m": null, "o": null, "s": "x"}')
        self.assertEqual(json.loads(collapsed), top_level(json.loads(text)))

    def test_top_level_array_keeps_its_scalars(self):
        self.assertEqual(auth._collapse_nested_json('[1, [2, [3]], "x", {}]'), '[1, null, "x", null]')

    def test_scalars_and_flat_values_are_unchanged(self):
        for text in ('{"exp": 1700000009}', "[]", "{}", "17", '"[{"', "null", " { } "):
            with self.subTest(text=text):
                self.assertEqual(auth._collapse_nested_json(text), text)

    def test_brackets_inside_strings_are_copied_verbatim(self):
        text = json.dumps({"name": "[" * 500 + "{" * 500 + "]}", "exp": 9})

        self.assertEqual(auth._collapse_nested_json(text), text)

    def test_brackets_inside_nested_strings_do_not_end_the_container(self):
        text = '{"m": ["]]}}", {"k": "[[{"}], "exp": 9}'

        self.assertEqual(auth._collapse_nested_json(text), '{"m": null, "exp": 9}')

    def test_escaped_quote_keeps_the_string_open(self):
        text = json.dumps({"name": 'a"' + "[" * 100, "exp": 9})
        self.assertIn('\\"', text)

        self.assertEqual(auth._collapse_nested_json(text), text)

    def test_escaped_backslash_before_quote_closes_the_string(self):
        # "a\\" ends after the escaped backslash; a scanner that treated the
        # quote as escaped would swallow the deep array that follows.
        text = '{"name": %s, "m": %s, "exp": 9}' % (json.dumps("a\\"), nested_arrays(40))
        self.assertIn('\\\\"', text)

        self.assertEqual(auth._collapse_nested_json(text), '{"name": "a\\\\", "m": null, "exp": 9}')

    def test_bracket_structure_json_rejects_returns_none(self):
        cases = {
            "unclosed openers": "[" * 64,
            "unclosed nested container": '{"exp": 9, "m": [[1]',
            "unclosed top level": '{"exp": 9, "m": [1]',
            "stray closer first": "]" + nested_arrays(40),
            "stray closer after the value": '{"exp": 9}]',
            "mismatched nested closer": '{"exp": 9, "m": [1}}',
            "mismatched top-level closer": '{"exp": 9]',
            "unterminated string": '{"exp": 9, "m": "[[',
            "unterminated nested string": '{"exp": 9, "m": ["x]}',
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    json.loads(text)
                self.assertIsNone(auth._collapse_nested_json(text))

    def test_trailing_data_stays_invalid(self):
        collapsed = auth._collapse_nested_json('{"exp": 9} [[1]]')

        self.assertEqual(collapsed, '{"exp": 9} [null]')
        with self.assertRaises(ValueError):
            json.loads(collapsed)

    def test_matches_json_loads_on_random_valid_documents(self):
        rng = random.Random(20261012)
        for case in range(400):
            value = random_json(rng, rng.randint(0, 8))
            text = json.dumps(value, ensure_ascii=rng.random() < 0.5)
            with self.subTest(case=case, text=text[:200]):
                collapsed = auth._collapse_nested_json(text)
                self.assertIsNotNone(collapsed)
                self.assertLessEqual(json_depth(collapsed), 1)
                self.assertEqual(json.loads(collapsed), top_level(value))


class NestingGuardTests(AuthCacheTestCase):
    def test_nesting_up_to_the_limit_still_bounds_ttl_by_exp(self):
        limit = auth._JWT_MAX_NESTING
        # The claim-set object itself is one level.
        token = make_jwt(None, raw_payload=claims_with('"m": %s' % nested_arrays(limit - 1), self.clock.wall + 9))

        auth.get_current_user(bearer(token))

        self.assertEqual(self.cached_ttl(token), 9.0)

    def test_nesting_beyond_the_limit_is_collapsed_and_exp_still_bounds_ttl(self):
        limit = auth._JWT_MAX_NESTING
        exp = self.clock.wall + 9
        cases = {
            "one level too deep": claims_with('"m": %s' % nested_arrays(limit), exp),
            "user_metadata 5000 deep": claims_with('"user_metadata": %s' % nested_arrays(5000), exp),
            "exp after the deep member": ('{"user_metadata": %s, "exp": %s}' % (nested_arrays(5000), json.dumps(exp))).encode(),
            "utf-16 encoded": claims_with('"m": %s' % nested_arrays(5000), exp).decode().encode("utf-16"),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                auth.clear_auth_cache()
                token = make_jwt(None, raw_payload=payload)
                with patch.object(auth.json, "loads", wraps=json.loads) as loads:
                    auth.get_current_user(bearer(token))
                # The parser only ever sees shallow text...
                self.assertTrue(loads.called)
                for call in loads.call_args_list:
                    self.assertLessEqual(json_depth(call.args[0]), limit)
                # ...yet the real exp still bounds the cache lifetime.
                self.assertEqual(self.cached_ttl(token), 9.0)

    def test_deeply_nested_token_is_not_cached_past_its_exp(self):
        # Regression: skipping exp on deep payloads fell back to the 60 second
        # ceiling, keeping a token that expires in 5s cached 55s past its exp.
        token = make_jwt(None, raw_payload=claims_with('"user_metadata": %s' % nested_arrays(40), self.clock.wall + 5))

        auth.get_current_user(bearer(token))
        self.assertLessEqual(self.cached_ttl(token), 5.0)

        self.clock.advance(5)
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)
        self.assertEqual(len(auth._auth_cache), 0)

    def test_unparseable_deep_payload_is_never_handed_to_the_parser(self):
        for label, payload in {
            "unclosed openers": b"[" * 100_000,
            "mismatched closer": claims_with('"m": %s}' % ("[" * 40), self.clock.wall + 9),
        }.items():
            with self.subTest(label):
                auth.clear_auth_cache()
                token = make_jwt(None, raw_payload=payload)
                with patch.object(auth.json, "loads", wraps=json.loads) as loads:
                    auth.get_current_user(bearer(token))
                loads.assert_not_called()
                # Treated like any other token without a usable exp.
                self.assertEqual(self.cached_ttl(token), 60.0)

    def test_collapsed_container_exp_is_not_mistaken_for_a_timestamp(self):
        # Collapsing a container-valued exp to 0 would read as "expired" and
        # stop caching; it must stay "no usable exp" like the shallow case.
        for label, exp_json in {
            "shallow container exp": "[1700000009]",
            "deep container exp": nested_arrays(40),
            "deep object exp": '{"v": %s}' % nested_arrays(40),
        }.items():
            with self.subTest(label):
                auth.clear_auth_cache()
                token = make_jwt(None, raw_payload=('{"sub": "u", "exp": %s}' % exp_json).encode())

                auth.get_current_user(bearer(token))

                self.assertEqual(self.cached_ttl(token), 60.0)

    def test_only_the_top_level_exp_counts(self):
        nested_exp = self.clock.wall + 9
        deep_padding = nested_arrays(40)
        cases = {
            "exp only inside nested metadata": (
                '{"sub": "u", "m": {"exp": %s}, "pad": %s}' % (json.dumps(nested_exp), deep_padding),
                60.0,
            ),
            # json.loads keeps the last duplicate; the collapsed text must too.
            "duplicate exp, last wins": (
                '{"exp": %s, "pad": %s, "exp": %s}' % (json.dumps(self.clock.wall + 3600), deep_padding, json.dumps(nested_exp)),
                9.0,
            ),
        }
        for label, (claims, expected_ttl) in cases.items():
            with self.subTest(label):
                auth.clear_auth_cache()
                token = make_jwt(None, raw_payload=claims.encode())

                auth.get_current_user(bearer(token))

                self.assertEqual(self.cached_ttl(token), expected_ttl)

    def test_exp_with_more_digits_than_int_conversion_allows_has_no_usable_exp(self):
        # Python caps int parsing at 4300 digits and raises ValueError.
        token = make_jwt(None, raw_payload=b'{"exp": 1' + b"0" * 5000 + b"}")

        auth.get_current_user(bearer(token))

        self.assertEqual(self.cached_ttl(token), 60.0)

    def test_shallow_payloads_are_still_parsed_for_exp(self):
        exp = self.clock.wall + 9
        cases = {
            "wide but shallow metadata": claims_with('"m": [%s]' % ",".join(['[{"k": [1]}]'] * 200), exp),
            "brackets inside strings": claims_with('"name": %s' % json.dumps("[{" * 500), exp),
            "escaped quote in a string": claims_with('"name": %s' % json.dumps('a"' + "[" * 100), exp),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                auth.clear_auth_cache()
                token = make_jwt(None, raw_payload=payload)

                auth.get_current_user(bearer(token))

                self.assertEqual(self.cached_ttl(token), 9.0)

    def test_payload_text_is_decoded_like_json_loads_bytes(self):
        # The pre-scan decodes the payload itself; it must accept exactly what
        # json.loads(bytes) accepted, BOMs and UTF-16/32 included.
        claims = json.dumps({"sub": "user-1", "exp": self.clock.wall + 9})
        cases = {
            "utf-8": claims.encode("utf-8"),
            "utf-8 with BOM": b"\xef\xbb\xbf" + claims.encode("utf-8"),
            "utf-16 with BOM": claims.encode("utf-16"),
            "utf-16-le without BOM": claims.encode("utf-16-le"),
            "utf-32": claims.encode("utf-32"),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                auth.clear_auth_cache()
                self.assertEqual(json.loads(payload)["exp"], self.clock.wall + 9)
                token = make_jwt(None, raw_payload=payload)

                auth.get_current_user(bearer(token))

                self.assertEqual(self.cached_ttl(token), 9.0)


# Runs in a child process because a C stack overflow kills the interpreter
# outright (SIGSEGV/SIGBUS); in-process it would take the whole test run down.
SMALL_STACK_SCRIPT = r"""
import base64, json, sys, threading, types

sys.path.insert(0, sys.argv[1])
# Only the claim parser is under test, so Supabase and Turso stay out of it.
config = types.ModuleType("config")
config.settings = types.SimpleNamespace(auth_cache_seconds=120)
database = types.ModuleType("database")
database.supabase = None
database.DatabaseConfigurationError = type("DatabaseConfigurationError", (RuntimeError,), {})
sys.modules.update(config=config, database=database)
import api.auth as auth

def token(payload):
    return "h." + base64.urlsafe_b64encode(payload).rstrip(b"=").decode() + ".s"

tokens = [
    # Valid JSON, ~13 KiB as a token: small enough for uvicorn's 16 KiB header
    # limit. Its exp must still be read, not dropped along with the nesting.
    token(b'{"exp": 1700000009, "user_metadata": ' + b"[" * 5000 + b"]" * 5000 + b"}"),
    token(b"[" * 100000),
    token(b'{"exp": 1700000009, "m": [[{"k": [1]}]]}'),
]
try:
    # Emulates a small worker-thread stack (musl defaults to 128 KiB).
    threading.stack_size(256 * 1024)
except (ValueError, RuntimeError):
    print("unsupported")
    sys.exit(0)
results = []
worker = threading.Thread(target=lambda: results.extend(auth._jwt_expiry(t) for t in tokens))
worker.start()
worker.join()
print(json.dumps(results))
"""


class SmallStackTests(unittest.TestCase):
    def test_deeply_nested_claims_cannot_crash_a_small_stack_worker_thread(self):
        backend_dir = Path(auth.__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, "-I", "-c", SMALL_STACK_SCRIPT, str(backend_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.stdout.strip() == "unsupported":
            self.skipTest("threading.stack_size is not adjustable on this platform")
        # A negative return code is the signal that killed the child.
        self.assertEqual(result.returncode, 0, f"child exited {result.returncode}: {result.stderr[-2000:]}")
        self.assertEqual(json.loads(result.stdout), [1700000009.0, None, 1700000009.0])


class HostileInputTests(AuthCacheTestCase):
    def test_lone_surrogate_token_gets_auth_errors_not_server_errors(self):
        # Not reachable through Starlette (headers are latin-1), but a direct
        # caller must still get the normal 401 rather than UnicodeEncodeError.
        token = "abc\ud800def"
        self.get_user.side_effect = [None, user_response("user-1")]

        with self.assertRaises(HTTPException) as raised:
            auth.get_current_user(bearer(token))
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(len(auth._auth_cache), 0)

        auth.get_current_user(bearer(token))
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)
        expected_key = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()
        self.assertEqual(list(auth._auth_cache), [expected_key])

    def test_base_exception_from_supabase_propagates_unchanged_and_is_not_cached(self):
        # Cancellation and interpreter shutdown are BaseExceptions; turning
        # them into a 401 would swallow them.
        class Cancelled(BaseException):
            pass

        token = self.jwt(3600)
        self.get_user.side_effect = [Cancelled(), user_response("user-1")]

        with self.assertRaises(Cancelled):
            auth.get_current_user(bearer(token))
        self.assertEqual(len(auth._auth_cache), 0)

        auth.get_current_user(bearer(token))
        auth.get_current_user(bearer(token))
        self.assertEqual(self.get_user.call_count, 2)

    def test_non_ascii_header_and_upstream_error_text_through_http(self):
        app = FastAPI()

        @app.get("/me")
        def me(user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
            return {"id": user.id}

        client = TestClient(app)
        for _ in range(2):
            response = client.get("/me", headers={"Authorization": b"Bearer tok\xe9n\xff-\x80"})
            self.assertEqual((response.status_code, response.json()), (200, {"id": "user-1"}))
        received = self.get_user.call_args.args[0]
        self.assertFalse(received.isascii())
        self.get_user.assert_called_once_with(received)
        self.assertEqual(list(auth._auth_cache), [hashlib.sha256(received.encode("utf-8")).hexdigest()])

        # An upstream error that quotes the token must not reach the client.
        secret = "leaky-token-value"
        self.get_user.side_effect = RuntimeError(f"rejected token {secret}")
        response = client.get("/me", headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Authentication failed. Sign in again and retry."})
        self.assertNotIn(secret, response.text)
        self.assertNotIn(cache_key(secret), auth._auth_cache)


class PublicInterfaceTests(AuthCacheTestCase):
    def test_public_names_and_signature_are_unchanged(self):
        self.assertIsInstance(auth.security, HTTPBearer)
        self.assertEqual(
            [field for field in auth.AuthenticatedUser.__dataclass_fields__],
            ["id", "access_token", "raw_user"],
        )
        # Routers rely on FastAPI running this in its threadpool, not the loop.
        self.assertFalse(inspect.iscoroutinefunction(auth.get_current_user))
        parameters = inspect.signature(auth.get_current_user).parameters
        self.assertEqual(list(parameters), ["credentials"])
        self.assertIs(parameters["credentials"].default.dependency, auth.security)
        self.assertTrue(callable(auth.clear_auth_cache))

    def test_fastapi_dependency_caches_and_keeps_error_responses(self):
        app = FastAPI()

        @app.get("/me")
        def me(user: auth.AuthenticatedUser = Depends(auth.get_current_user)):
            return {"id": user.id}

        client = TestClient(app)
        good = self.jwt(3600)
        for _ in range(3):
            response = client.get("/me", headers={"Authorization": f"Bearer {good}"})
            self.assertEqual((response.status_code, response.json()), (200, {"id": "user-1"}))
        self.get_user.assert_called_once_with(good)

        cases = [
            (None, 401, "Invalid access token or expired session"),
            (DatabaseConfigurationError("Supabase is not configured"), 503, "Supabase is not configured"),
            (RuntimeError("boom"), 401, "Authentication failed. Sign in again and retry."),
        ]
        for index, (outcome, status_code, detail) in enumerate(cases):
            with self.subTest(status_code=status_code, detail=detail):
                if isinstance(outcome, BaseException):
                    self.get_user.side_effect = outcome
                else:
                    self.get_user.side_effect = None
                    self.get_user.return_value = outcome
                token = self.jwt(3600, sub=f"bad-{index}")
                response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual((response.status_code, response.json()), (status_code, {"detail": detail}))
                self.assertNotIn(cache_key(token), auth._auth_cache)


if __name__ == "__main__":
    unittest.main()
