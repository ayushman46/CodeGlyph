"""Database boundaries for Supabase Auth and Turso application data.

Supabase remains the identity provider. Repository records, indexing jobs,
source chunks, vector embeddings, metadata, and conversations live in Turso.

Two interchangeable stores implement the same small async interface:

* ``TursoHttpStore`` talks to a remote Turso/libSQL server over the Hrana
  HTTP protocol with a pooled ``httpx.AsyncClient``. It never blocks the event
  loop, so ingestion writes, status polls, and streamed answers run
  concurrently. This is the production path for ``libsql://`` and ``https://``
  database URLs.
* ``TursoStore`` wraps the embedded ``libsql`` client for local ``file:``
  databases used by tests and offline development. The embedded client holds
  the GIL while a statement runs, which is why it is not used for the remote
  production database.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import random
import re
import weakref
from array import array
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
import libsql
from supabase import create_client

from config import settings

logger = logging.getLogger(__name__)


class DatabaseConfigurationError(RuntimeError):
    """Raised when a required database service is unavailable or unconfigured."""


class DatabaseUnavailableError(DatabaseConfigurationError):
    """Raised after a bounded retry confirms the Turso connection is unavailable."""


class DatabaseStatementError(RuntimeError):
    """A non-transient SQL error returned by Turso (constraint, syntax, schema)."""


class TransientDatabaseError(RuntimeError):
    """A retryable transport failure (timeout, reset, HTTP 429/5xx)."""


class DeferredSupabaseClient:
    def __init__(self, message: str):
        self.message = message

    def __getattr__(self, _name):
        raise DatabaseConfigurationError(self.message)


def get_supabase_client():
    """Return the public-key client used exclusively to validate user sessions."""
    url = settings.supabase_url.strip()
    key = settings.supabase_key.strip()
    message = (
        "Supabase authentication is not configured. Set SUPABASE_URL and SUPABASE_KEY "
        "in the backend environment, then restart the service."
    )
    if not url or not key or "your_supabase" in url or "your_supabase" in key:
        return DeferredSupabaseClient(message)
    try:
        return create_client(url, key)
    except Exception:
        logger.exception("Could not initialize the Supabase authentication client")
        return DeferredSupabaseClient(message)


supabase = get_supabase_client()

JSON_COLUMNS = {
    "symbols", "citations", "tool_calls", "tech_stack", "file_summaries",
    "excluded_reasons", "excluded_paths", "selection_json",
}
_JSON_LIST_COLUMNS = {"symbols", "citations", "tool_calls", "tech_stack", "excluded_paths"}


def embedding_blob(vector: Any) -> bytes:
    """Pack a vector as little-endian float32 bytes for ``vector32(?)``.

    A packed blob is less than half the size of the JSON text representation
    and avoids formatting 2,048 floats per chunk on the worker.
    """
    if isinstance(vector, (bytes, bytearray, memoryview)):
        return bytes(vector)
    packed = vector if isinstance(vector, array) and vector.typecode == "f" else array("f", vector)
    if packed.itemsize != 4:  # pragma: no cover - CPython float32 is always 4 bytes
        raise ValueError("float32 vectors must use 4-byte items")
    return packed.tobytes()


def _embedding_json(vector: Any) -> str:
    """Encode packed or ordinary vectors as compact JSON (embedding cache rows)."""
    values = vector.tolist() if isinstance(vector, array) else list(vector)
    return json.dumps(values, separators=(",", ":"))


def _parse_json_columns(value: dict[str, Any]) -> dict[str, Any]:
    for column in JSON_COLUMNS:
        raw = value.get(column)
        if isinstance(raw, str):
            try:
                value[column] = json.loads(raw)
            except json.JSONDecodeError:
                value[column] = [] if column in _JSON_LIST_COLUMNS else {}
    return value


def is_read_statement(sql: str) -> bool:
    normalized = sql.lstrip().upper()
    if normalized.startswith(("SELECT", "EXPLAIN", "PRAGMA")):
        return True
    # Read-only window-function CTEs are used by retrieval. Treat WITH as a
    # read unless the statement contains a mutating verb.
    return normalized.startswith("WITH") and not re.search(
        r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", normalized
    )


_TRANSIENT_MARKERS = (
    "database is locked", "database is busy", "sqlite_busy", "busy", "timeout", "timed out",
    "connection", "temporarily unavailable", "http 429", "http 500", "http 502",
    "http 503", "http 504", "stream not found", "broken pipe", "connection reset",
    "hrana", "transport", "socket", "unexpected eof", "eof while",
)


def is_transient_error(error: BaseException) -> bool:
    if isinstance(error, TransientDatabaseError):
        return True
    if isinstance(error, (DatabaseStatementError, DatabaseConfigurationError)):
        return any(marker in str(error).lower() for marker in ("database is locked", "sqlite_busy", "database is busy"))
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def retry_delay(attempt: int) -> float:
    return min(1.5, 0.15 * (2 ** attempt)) + random.uniform(0, 0.1)


@dataclass(frozen=True)
class Statement:
    sql: str
    args: list[Any]


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    rows_affected: int


class _StoreHelpers:
    """Domain helpers shared by the HTTP and embedded stores."""

    async def fetch_all(self, sql: str, args: list[Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        return (await self.execute(sql, args)).rows  # type: ignore[attr-defined]

    async def fetch_one(self, sql: str, args: list[Any] | tuple[Any, ...] | None = None) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, args)
        return rows[0] if rows else None

    async def insert_chunks(self, chunks: list[dict[str, Any]]) -> None:
        """Insert a bounded chunk batch; vectors are packed float32 blobs."""
        vector_sql = (
            "INSERT OR IGNORE INTO chunks (id, repo_id, file_path, start_line, end_line, language, symbols, content, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, vector32(?))"
        )
        plain_sql = (
            "INSERT OR IGNORE INTO chunks (id, repo_id, file_path, start_line, end_line, language, symbols, content, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)"
        )
        statements: list[Statement] = []
        for chunk in chunks:
            values = [
                chunk["id"], chunk["repo_id"], chunk["file_path"], chunk["start_line"],
                chunk["end_line"], chunk["language"], json.dumps(chunk.get("symbols") or []), chunk["content"],
            ]
            if chunk.get("embedding") is None:
                statements.append(Statement(plain_sql, values))
            else:
                statements.append(Statement(vector_sql, values + [embedding_blob(chunk["embedding"])]))
        await self.batch(statements)  # type: ignore[attr-defined]

    async def update_chunk_embeddings(self, chunks: list[dict[str, Any]]) -> None:
        """Attach vectors to already keyword-indexed chunks in one bounded batch."""
        sql = "UPDATE chunks SET embedding = vector32(?) WHERE id = ? AND repo_id = ?"
        statements = [
            Statement(sql, [embedding_blob(chunk["embedding"]), chunk["id"], chunk["repo_id"]])
            for chunk in chunks
            if chunk.get("embedding") is not None
        ]
        if statements:
            await self.batch(statements)  # type: ignore[attr-defined]

    async def upsert_file_vectors(self, repo_id: str, vectors: list[tuple[str, Any, int]], updated_at: str) -> None:
        """Persist per-file centroid vectors used for file-level semantic search."""
        sql = (
            "INSERT INTO file_vectors (repo_id, file_path, embedding, chunk_count, updated_at) "
            "VALUES (?, ?, vector32(?), ?, ?) ON CONFLICT(repo_id, file_path) DO UPDATE SET "
            "embedding = excluded.embedding, chunk_count = excluded.chunk_count, updated_at = excluded.updated_at"
        )
        statements = [
            Statement(sql, [repo_id, path, embedding_blob(vector), int(count), updated_at])
            for path, vector, count in vectors
        ]
        if statements:
            await self.batch(statements)  # type: ignore[attr-defined]

    async def get_embedding_cache(self, content_hashes: list[str]) -> dict[str, list[float]]:
        """Return cached vectors for a bounded set of passage hashes."""
        hashes = list(dict.fromkeys(value for value in content_hashes if value))
        if not hashes:
            return {}
        cached: dict[str, list[float]] = {}
        # Keep each IN-list bounded so a large buffer never exceeds SQLite's
        # parameter limit or produces one oversized request.
        for offset in range(0, len(hashes), 200):
            window = hashes[offset:offset + 200]
            placeholders = ", ".join("?" for _ in window)
            rows = await self.fetch_all(
                "SELECT content_hash, embedding_json FROM embedding_cache "
                "WHERE model = ? AND dimension = ? AND content_hash IN (" + placeholders + ")",
                [settings.embedding_model, settings.embedding_dimension, *window],
            )
            for row in rows:
                try:
                    cached[str(row["content_hash"])] = json.loads(row["embedding_json"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    logger.warning("Ignoring malformed embedding cache row")
        return cached

    async def save_embedding_cache(self, records: list[dict[str, Any]]) -> None:
        """Persist only vectors generated by this bounded batch."""
        statements = []
        for record in records:
            if not record.get("content_hash") or record.get("embedding") is None:
                continue
            statements.append(Statement(
                "INSERT OR REPLACE INTO embedding_cache "
                "(content_hash, model, dimension, embedding_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                [
                    record["content_hash"], settings.embedding_model, settings.embedding_dimension,
                    _embedding_json(record["embedding"]), record["updated_at"],
                ],
            ))
        if statements:
            await self.batch(statements)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Remote Turso over Hrana HTTP
# ---------------------------------------------------------------------------


def pipeline_url(database_url: str) -> str:
    """Translate a Turso/libSQL database URL into its Hrana HTTP endpoint."""
    url = database_url.strip()
    for prefix, replacement in (("libsql://", "https://"), ("wss://", "https://"), ("ws://", "http://")):
        if url.startswith(prefix):
            url = replacement + url[len(prefix):]
            break
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DatabaseConfigurationError("TURSO_DATABASE_URL must be a libsql://, https://, or file: URL.")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/v2/pipeline"


def encode_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Turso cannot store non-finite floats.")
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, array):
        return {"type": "blob", "base64": base64.b64encode(embedding_blob(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def decode_value(cell: dict[str, Any]) -> Any:
    kind = cell.get("type")
    if kind == "null" or kind is None:
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    if kind == "text":
        return cell.get("value", "")
    if kind == "blob":
        raw = str(cell.get("base64") or "")
        return base64.b64decode(raw + "=" * (-len(raw) % 4))
    return cell.get("value")


def _statement_payload(sql: str, args: Iterable[Any]) -> dict[str, Any]:
    return {"sql": sql, "args": [encode_value(value) for value in args]}


def _result_to_query(sql: str, result: dict[str, Any]) -> QueryResult:
    columns = [str(column.get("name") or "") for column in result.get("cols") or []]
    rows = [
        _parse_json_columns(dict(zip(columns, (decode_value(cell) for cell in row), strict=True)))
        for row in result.get("rows") or []
    ]
    affected = int(result.get("affected_row_count") or 0)
    return QueryResult(rows=rows, rows_affected=len(rows) if is_read_statement(sql) else affected)


class TursoHttpStore(_StoreHelpers):
    """Asynchronous, parameterized Turso client using the Hrana HTTP pipeline."""

    # Bound one HTTP request so a large chunk or embedding batch never turns
    # into an oversized payload. Each sub-batch remains a single transaction.
    _MAX_BATCH_STATEMENTS = 120
    _MAX_BATCH_BYTES = 3_500_000

    def __init__(self, url: str, auth_token: str, *, timeout_seconds: float | None = None):
        self._endpoint = pipeline_url(url)
        self._auth_token = auth_token.strip()
        self._timeout = float(timeout_seconds or settings.turso_http_timeout_seconds)
        self._clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()

    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or client.is_closed:
            headers = {"Content-Type": "application/json", "User-Agent": "codebase-intel/3"}
            if self._auth_token:
                headers["Authorization"] = f"Bearer {self._auth_token}"
            client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=12, keepalive_expiry=30.0),
            )
            self._clients[loop] = client
        return client

    async def _post(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            response = await self._client().post(
                self._endpoint, json={"requests": [*requests, {"type": "close"}]},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise _NotSentError("Turso connection could not be established.") from error
        except httpx.TimeoutException as error:
            raise TransientDatabaseError("Turso request timed out.") from error
        except httpx.TransportError as error:
            raise TransientDatabaseError("Turso transport failed.") from error
        if response.status_code in {401, 403}:
            raise DatabaseConfigurationError(
                "Turso rejected the database credential. Replace TURSO_AUTH_TOKEN and restart the backend."
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientDatabaseError(f"Turso returned HTTP {response.status_code}.")
        if response.status_code >= 400:
            raise DatabaseStatementError(f"Turso returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as error:
            raise TransientDatabaseError("Turso returned an invalid response.") from error
        return list(payload.get("results") or [])[: len(requests)]

    @staticmethod
    def _raise_result_error(result: dict[str, Any]) -> None:
        if result.get("type") == "ok":
            return
        error = result.get("error") or {}
        message = str(error.get("message") or "Turso statement failed.")
        if any(marker in message.lower() for marker in ("database is locked", "sqlite_busy", "database is busy")):
            raise TransientDatabaseError(message)
        raise DatabaseStatementError(message)

    async def _run_with_retry(self, operation, *, retry_ambiguous: bool):
        attempts = 3
        for attempt in range(attempts):
            try:
                return await operation()
            except _NotSentError as error:
                # The request never reached Turso, so even a non-idempotent
                # write is safe to retry.
                if attempt == attempts - 1:
                    raise DatabaseUnavailableError(
                        "The Turso database connection is temporarily unavailable. Please retry shortly."
                    ) from error
            except TransientDatabaseError as error:
                if not retry_ambiguous or attempt == attempts - 1:
                    raise DatabaseUnavailableError(
                        "The Turso database connection is temporarily unavailable. Please retry shortly."
                    ) from error
            await asyncio.sleep(retry_delay(attempt))
        raise RuntimeError("Turso retry loop ended unexpectedly.")  # pragma: no cover

    async def _execute(self, sql: str, args: list[Any] | tuple[Any, ...] | None, *, idempotent: bool) -> QueryResult:
        arguments = list(args or [])

        async def operation():
            results = await self._post([{"type": "execute", "stmt": _statement_payload(sql, arguments)}])
            if not results:
                raise TransientDatabaseError("Turso returned no statement result.")
            self._raise_result_error(results[0])
            return _result_to_query(sql, results[0]["response"]["result"])

        return await self._run_with_retry(operation, retry_ambiguous=idempotent or is_read_statement(sql))

    async def execute(self, sql: str, args: list[Any] | tuple[Any, ...] | None = None) -> QueryResult:
        """Execute one statement. Reads retry; ambiguous writes do not."""
        return await self._execute(sql, args, idempotent=False)

    async def execute_idempotent(self, sql: str, args: list[Any] | tuple[Any, ...] | None = None) -> QueryResult:
        """Execute an explicitly idempotent write with bounded retries."""
        return await self._execute(sql, args, idempotent=True)

    def _split_batches(self, statements: list[Statement]) -> list[list[Statement]]:
        groups: list[list[Statement]] = []
        current: list[Statement] = []
        current_bytes = 0
        for statement in statements:
            size = len(statement.sql) + sum(
                len(value) * 4 // 3 if isinstance(value, (bytes, bytearray)) else len(str(value))
                for value in statement.args
            )
            if current and (len(current) >= self._MAX_BATCH_STATEMENTS or current_bytes + size > self._MAX_BATCH_BYTES):
                groups.append(current)
                current, current_bytes = [], 0
            current.append(statement)
            current_bytes += size
        if current:
            groups.append(current)
        return groups

    async def batch(self, statements: Iterable[Statement]) -> None:
        """Run idempotent writes as bounded atomic transactions with retries."""
        statements = list(statements)
        for group in self._split_batches(statements):
            steps: list[dict[str, Any]] = [{"stmt": {"sql": "BEGIN IMMEDIATE"}}]
            for index, statement in enumerate(group, start=1):
                steps.append({
                    "stmt": _statement_payload(statement.sql, statement.args),
                    "condition": {"type": "ok", "step": index - 1},
                })
            commit_index = len(steps)
            steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": commit_index - 1}})
            steps.append({"stmt": {"sql": "ROLLBACK"}, "condition": {"type": "not", "cond": {"type": "ok", "step": commit_index}}})

            async def operation(steps=steps):
                results = await self._post([{"type": "batch", "batch": {"steps": steps}}])
                if not results:
                    raise TransientDatabaseError("Turso returned no batch result.")
                self._raise_result_error(results[0])
                outcome = results[0]["response"]["result"]
                for error in outcome.get("step_errors") or []:
                    if error:
                        message = str(error.get("message") or "Turso batch statement failed.")
                        if any(marker in message.lower() for marker in ("database is locked", "sqlite_busy", "database is busy")):
                            raise TransientDatabaseError(message)
                        raise DatabaseStatementError(message)

            await self._run_with_retry(operation, retry_ambiguous=True)

    async def close(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - close is awaited from a loop
            return
        client = self._clients.pop(loop, None)
        if client is not None:
            await client.aclose()


class _NotSentError(TransientDatabaseError):
    """The HTTP request was not delivered to Turso; any statement may retry."""


# ---------------------------------------------------------------------------
# Embedded libsql (local file databases for tests and offline development)
# ---------------------------------------------------------------------------


class TursoStore(_StoreHelpers):
    """Embedded libsql client for ``file:`` databases.

    Ownership is enforced by including the authenticated user id in each
    repository and conversation query, exactly as with the HTTP store.
    """

    def __init__(self, url: str, auth_token: str):
        self._database_url = url
        self._auth_token = auth_token
        self._client = self._connect_sync()
        self._lock = asyncio.Lock()

    def _connect_sync(self):
        return libsql.connect(
            database=self._database_url,
            auth_token=self._auth_token,
            _check_same_thread=False,
        )

    @staticmethod
    def _row_to_dict(columns: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        return _parse_json_columns(dict(zip(columns, row, strict=True)))

    async def execute(self, sql: str, args: list[Any] | tuple[Any, ...] | None = None):
        return await self._execute_with_retry(sql, args, retry_idempotent_write=False)

    async def execute_idempotent(self, sql: str, args: list[Any] | tuple[Any, ...] | None = None):
        return await self._execute_with_retry(sql, args, retry_idempotent_write=True)

    async def _execute_with_retry(
        self,
        sql: str,
        args: list[Any] | tuple[Any, ...] | None = None,
        *,
        retry_idempotent_write: bool,
    ):
        arguments = list(args or [])
        attempts = 3 if self._is_read_statement(sql) or retry_idempotent_write else 1
        for attempt in range(attempts):
            try:
                async with self._lock:
                    return await asyncio.to_thread(self._execute_sync, sql, arguments)
            except Exception as error:
                if not self._is_transient_error(error):
                    raise
                try:
                    await self._refresh_connection()
                except Exception as reconnect_error:
                    raise DatabaseUnavailableError(
                        "Turso connection was lost and could not be restored. Please retry shortly."
                    ) from reconnect_error
                if attempt == attempts - 1:
                    raise DatabaseUnavailableError(
                        "Turso connection was lost while writing. Please retry shortly."
                        if retry_idempotent_write
                        else "Turso connection was lost while processing the request. Please retry shortly."
                    ) from error
                await asyncio.sleep(retry_delay(attempt))
        raise RuntimeError("Turso retry loop ended unexpectedly.")  # pragma: no cover

    _is_read_statement = staticmethod(is_read_statement)
    _is_transient_error = staticmethod(is_transient_error)
    _retry_delay = staticmethod(retry_delay)

    def _execute_sync(self, sql: str, args: list[Any]) -> QueryResult:
        cursor = self._client.execute(sql, args)
        columns = [item[0] for item in cursor.description] if cursor.description else []
        rows = [self._row_to_dict(columns, row) for row in cursor.fetchall()] if columns else []
        affected = cursor.rowcount if cursor.rowcount >= 0 else len(rows)
        if not self._is_read_statement(sql):
            self._client.commit()
        return QueryResult(rows=rows, rows_affected=affected)

    async def _refresh_connection(self) -> None:
        async with self._lock:
            old_client = self._client
            try:
                await asyncio.to_thread(old_client.close)
            except Exception:
                logger.debug("Ignoring failure while closing stale Turso client", exc_info=True)
            self._client = await asyncio.to_thread(self._connect_sync)

    async def batch(self, statements: Iterable[Statement]) -> None:
        statements = list(statements)
        if not statements:
            return
        grouped: dict[str, list[list[Any]]] = {}
        for statement in statements:
            grouped.setdefault(statement.sql, []).append(statement.args)
        for attempt in range(3):
            try:
                async with self._lock:
                    await asyncio.to_thread(self._batch_sync, grouped)
                return
            except Exception as error:
                if not self._is_transient_error(error):
                    raise
                try:
                    await self._refresh_connection()
                except Exception as reconnect_error:
                    raise DatabaseUnavailableError(
                        "Turso connection was lost and could not be restored. Please retry shortly."
                    ) from reconnect_error
                if attempt == 2:
                    raise DatabaseUnavailableError(
                        "Turso connection was lost while writing. Please retry shortly."
                    ) from error
                await asyncio.sleep(self._retry_delay(attempt))

    def _batch_sync(self, grouped: dict[str, list[list[Any]]]) -> None:
        for sql, argument_sets in grouped.items():
            self._client.executemany(sql, argument_sets)
        self._client.commit()

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._client.close)


def _is_embedded_url(url: str) -> bool:
    return url.startswith("file:") or url == ":memory:" or "://" not in url


def _is_local_http(url: str) -> bool:
    host = (urlparse(url.replace("libsql://", "https://")).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


@lru_cache(maxsize=1)
def get_turso_store() -> TursoHttpStore | TursoStore:
    url = settings.turso_database_url.strip()
    token = settings.turso_auth_token.strip()
    if not url or (not token and not _is_embedded_url(url) and not _is_local_http(url)):
        raise DatabaseConfigurationError(
            "Turso is not configured. Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in the backend environment."
        )
    if not _is_embedded_url(url):
        return TursoHttpStore(url, token)
    try:
        return TursoStore(url, token)
    except BaseException as error:
        # libsql's Rust layer can raise pyo3_runtime.PanicException when the
        # host has no usable certificate store. Convert it into the same safe
        # dependency error as an ordinary connection failure.
        if type(error).__name__ != "PanicException":
            raise
        raise DatabaseConfigurationError("Could not initialize the Turso database client.") from error


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

REQUIRED_TABLES = (
    "repos", "chunks", "repo_files", "repo_dependencies", "repo_coverage",
    "ingestion_jobs", "chat_messages", "kt_cache", "account_entitlements", "billing_orders",
    "repo_index_state", "ingestion_job_meta", "embedding_cache", "ingestion_metrics",
    "repo_snapshots", "file_vectors", "repo_index_runs", "edit_proposals",
)

# Each migration runs only when one of its marker tables is missing, so a
# populated production database never re-runs the expensive FTS rebuild in
# 00_init.sql. Every statement in these files is additive and idempotent.
MIGRATIONS: tuple[tuple[str, frozenset[str]], ...] = (
    ("00_init.sql", frozenset({"repos", "chunks", "chunks_fts", "ingestion_jobs", "kt_cache", "chat_messages"})),
    ("01_trust_features.sql", frozenset({"repo_files", "repo_dependencies", "repo_coverage"})),
    ("02_billing.sql", frozenset({"account_entitlements", "billing_orders"})),
    ("03_github.sql", frozenset({"user_github_tokens", "github_oauth_states", "github_change_operations"})),
    ("05_performance.sql", frozenset({"repo_index_state", "ingestion_job_meta", "embedding_cache", "ingestion_metrics"})),
    ("06_scale_and_editing.sql", frozenset({"repo_snapshots", "file_vectors", "repo_index_runs", "edit_proposals"})),
)


def migrations_directory() -> Path:
    configured = settings.migrations_dir.strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "turso"


def split_sql_script(script: str) -> list[str]:
    """Split a migration file into statements, keeping trigger bodies intact."""
    statements: list[str] = []
    buffer: list[str] = []
    in_trigger = False
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buffer.append(raw_line.rstrip())
        upper = line.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper == "END;" or upper.endswith(" END;"):
                statements.append("\n".join(buffer))
                buffer, in_trigger = [], False
        elif line.endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    if buffer:
        statements.append("\n".join(buffer))
    return [statement for statement in statements if not statement.strip().upper().startswith("PRAGMA")]


async def _existing_tables(store) -> set[str]:
    rows = await store.fetch_all("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    return {str(row.get("name")) for row in rows}


async def apply_migrations(store) -> list[str]:
    """Apply missing additive migrations and return the files that ran."""
    directory = migrations_directory()
    if not directory.is_dir():
        logger.warning("Turso migrations directory %s is unavailable; skipping auto-migration", directory)
        return []
    existing = await _existing_tables(store)
    applied: list[str] = []
    for filename, markers in MIGRATIONS:
        if markers <= existing:
            continue
        path = directory / filename
        if not path.is_file():
            logger.warning("Turso migration %s is missing; skipping", filename)
            continue
        for statement in split_sql_script(path.read_text(encoding="utf-8")):
            await store.execute(statement)
        applied.append(filename)
        existing = await _existing_tables(store)
    if applied:
        logger.info("Applied Turso migrations: %s", ", ".join(applied))
    return applied


_turso_schema_verified = False
_turso_schema_lock = asyncio.Lock()


async def assert_turso_schema() -> None:
    """Bootstrap missing tables, then fail early with a migration instruction."""
    global _turso_schema_verified
    if _turso_schema_verified:
        return
    async with _turso_schema_lock:
        if _turso_schema_verified:
            return
        try:
            store = get_turso_store()
            if settings.auto_migrate:
                await apply_migrations(store)
            present = await _existing_tables(store)
            missing = [table for table in REQUIRED_TABLES if table not in present]
            if missing:
                raise DatabaseConfigurationError(
                    "Turso schema is missing required tables: " + ", ".join(missing)
                    + ". Run the SQL files in turso/ (00 through 06) or enable AUTO_MIGRATE."
                )
        except DatabaseConfigurationError:
            raise
        except BaseException as error:
            if type(error).__name__ != "PanicException":
                raise
            raise DatabaseConfigurationError(
                "Turso schema is not initialized or is unavailable. Run the SQL files in turso/ "
                "(00 through 06) in the Turso SQL shell, then restart the backend."
            ) from error
        _turso_schema_verified = True


def reset_schema_verification() -> None:
    """Allow tests to re-run the bootstrap against a fresh database."""
    global _turso_schema_verified
    _turso_schema_verified = False


def explain_database_error(error: Exception) -> str:
    """Return safe, actionable messages without exposing database credentials or SQL."""
    message = str(error).lower()
    if isinstance(error, DatabaseUnavailableError) or isinstance(error, TransientDatabaseError) or any(marker in message for marker in (
        "hrana", "stream not found", "broken pipe", "connection reset", "temporarily unavailable",
    )):
        return "The Turso database connection is temporarily unavailable. Please retry in a moment."
    if "no such table" in message or "does not exist" in message:
        return "Turso schema is not initialized. Run the SQL files in turso/ or enable AUTO_MIGRATE, then restart the backend."
    if "vector" in message and ("dimension" in message or "length" in message):
        return (
            "The configured embedding dimension does not match the Turso schema. "
            "Set EMBEDDING_DIMENSION=2048 and run the current turso/00_init.sql."
        )
    if "unauthorized" in message or "credential" in message or ("auth" in message and "token" in message):
        return "Turso rejected the database credential. Replace TURSO_AUTH_TOKEN and restart the backend."
    logger.error("Unhandled Turso database error: %s", type(error).__name__)
    return "The database could not complete that request. Please try again shortly."
