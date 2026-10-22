import asyncio
import time
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    # A strong, low-latency MoE model for source-grounded RAG. It has far fewer
    # active parameters than Ultra while retaining long-context coding support.
    nemotron_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    detailed_nemotron_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    # Code editing (plan, patch, self-review). Measured on live ky edits:
    # Ultra produced and applied 4/4 edits with 3/4 accepted, Super VL 2/4
    # accepted, and Lightning scored worst and often returned empty content,
    # so Super VL is the fallback when the Ultra endpoint is unavailable.
    # Ultra emits about 67 tokens/s on the hosted endpoint (Super VL ~100).
    code_editing_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    code_editing_fallback_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    embedding_model: str = "nvidia/nemotron-3-embed-1b"
    embedding_dimension: int = 2048
    # Multi-file edits can generate several thousand tokens in one request.
    nvidia_timeout_seconds: float = 150.0
    # Application-side ceiling for hosted NVIDIA calls. This is deliberately
    # configurable because NVIDIA assigns limits per account/model and may
    # show a different value in the API Catalog. Forty is the common free
    # prototype ceiling; operators should set this to the value shown for
    # their key rather than assuming it increases NVIDIA's quota.
    nvidia_calls_per_minute: int = 40
    # Preserve a few embedding slots for interactive questions while a large
    # repository is being indexed in the background.
    query_embedding_reserve_per_minute: int = 8
    # Answers are source-grounded and streamed, so a slightly larger budget
    # gives complete explanations without making the first token slower.
    nvidia_enable_thinking: bool = False
    answer_max_tokens: int = 1_400
    detailed_nvidia_enable_thinking: bool = True
    detailed_answer_max_tokens: int = 3_000
    code_editing_enable_thinking: bool = False
    # Multi-file patches are JSON with exact search/replace hunks. A small
    # output budget truncates the JSON and makes every non-trivial edit fail
    # validation, so the editor gets a generous ceiling. The hosted endpoint
    # was measured emitting 23.9k tokens in one completion; 16k covers eight
    # modified files plus a created test file.
    code_editing_answer_max_tokens: int = 16_384
    code_editing_plan_max_tokens: int = 1_500
    code_editing_retry_attempts: int = 3
    # Editing calls only, and only the ceiling: each patch call gets an
    # output budget sized to the files it may change (at least 4,096
    # tokens) and a timeout sized to that budget at a conservative
    # 40 tokens/s plus 30 s, so a doomed generation for one small file
    # stops after about two minutes instead of five.
    code_editing_timeout_seconds: float = 300.0
    # Wall-clock budget for one whole edit (plan, patch, repair). A repair
    # round is not started when less than 30 s of it is left, and every
    # model call (fallback model included) ends by this deadline.
    editing_deadline_seconds: float = 300.0
    # One verification-first review call after the deterministic quality
    # gates. Its fixes are applied only when they address a numbered
    # automatic finding or quote the wrong code, and they are re-checked.
    editing_self_review: bool = True
    # One file, one hunk of at most this many changed lines, and every
    # deterministic check passing: the review is skipped (0 never skips).
    editing_review_trivial_lines: int = 8
    editing_review_max_tokens: int = 4_096
    # Measured review latencies on live ky edits: Super VL 4.8-32.7 s (n=9,
    # median 9.3 s; the two runs that reached the old 90 s limit returned
    # nothing), Ultra 12.8-26.5 s (n=5). 45 s covers every completed review
    # with margin and halves the worst-case wait.
    editing_review_timeout_seconds: float = 45.0
    # The review is skipped when the edit itself already took this long.
    editing_review_deadline_seconds: float = 240.0
    # A rename's usages beyond the model-written files are updated by the
    # exact rename codemod; at most this many files in total.
    editing_max_mechanical_files: int = 20
    # Files one pull request may change (20 mechanical + 3 new + 1).
    github_max_pr_files: int = 24
    # Per-file syntax check ceiling (memory on the 512 MB instance).
    syntax_check_max_bytes: int = 1_000_000
    # Native esbuild binary for exact JS/TS/CSS parsing; empty auto-detects
    # frontend/node_modules/@esbuild/<os>-<arch>/bin/esbuild. "off" disables.
    esbuild_path: str = ""
    # Interactive answers fail fast enough to keep the chat responsive. Long
    # embedding jobs retain their separate retry budget below.
    answer_retry_attempts: int = 2
    # Keep hosted embedding requests bounded by both count and characters.
    # Larger batches reduce provider round trips; the character ceiling keeps
    # a single request safe for code-heavy chunks and a 512 MB worker.
    embedding_batch_size: int = 32
    embedding_min_batch_size: int = 4
    # Roughly 40k tokens of UTF-8 source at the hosted NIM batch boundary.
    # The embedder still adapts downward automatically if a catalog endpoint
    # advertises a smaller request budget.
    embedding_batch_max_characters: int = 160_000
    # Caching every 2,048-float vector doubles the temporary local database
    # footprint for very large repositories. Keep the cache for small and
    # medium indexes where it materially speeds re-indexing, and stream large
    # indexes without retaining a second copy of their vectors.
    embedding_cache_max_repository_bytes: int = 25_000_000
    # Aggregate chunks from small files before embedding. This avoids one
    # under-filled provider request per file while keeping peak memory bounded
    # on the 512 MB Render instance.
    # 256 chunks amortizes database/progress writes for large repositories.
    # At the 12k-character chunk ceiling this is still only a few MB of source
    # text plus one provider batch of vectors, well inside the Render margin.
    embedding_chunk_buffer_size: int = 256
    # Shared hosted capacity can return a short-lived 503. Five attempts with
    # backoff provide a 30-second recovery window before a job is failed.
    embedding_retry_attempts: int = 5
    embedding_retry_base_seconds: float = 2.0
    # Progress is user-facing metadata; cancellation is still checked for
    # every batch, while these less frequent writes reduce Turso contention.
    embedding_progress_interval_batches: int = 4
    embedding_heartbeat_interval_batches: int = 2
    # Persisting a few hundred chunks per transaction materially reduces
    # Turso round trips for large repositories while staying below the
    # provider's request-size limits. The pipeline still checks cancellation
    # between batches.
    chunk_insert_batch_size: int = 250
    # Two concurrent chunkers improve throughput for ordinary repositories.
    # Files above the large-file threshold are processed serially so a Render
    # 512 MB instance never holds several 50 MB source files at once.
    ingestion_chunk_workers: int = 2
    ingestion_large_file_serial_bytes: int = 8_000_000
    # Keep a substantial RSS margin below Render's 512 MB limit. The worker
    # shrinks buffers and pauses new provider work when these thresholds are
    # crossed instead of intentionally approaching an OOM kill.
    memory_target_mb: int = 350
    memory_warning_mb: int = 400
    memory_critical_mb: int = 450
    # One bounded producer, one embedding worker, and one database writer is
    # the safe default for the single 512 MB Render service.
    embedding_concurrency: int = 1
    ingestion_metrics_enabled: bool = True
    supabase_url: str = ""
    supabase_key: str = ""
    # Verified Supabase sessions are cached briefly so status polling does not
    # pay a Supabase round trip on every request.
    auth_cache_seconds: int = 120
    # Supabase remains the authentication provider. All application data is
    # stored through this server-only Turso connection.
    turso_database_url: str = ""
    turso_auth_token: str = ""
    turso_http_timeout_seconds: float = 60.0
    # Additive, idempotent migrations in turso/ are applied on first use so a
    # deployment never needs manual SQL. Set false to manage schema manually.
    auto_migrate: bool = True
    migrations_dir: str = ""
    # Billing is server-only. The public Razorpay key is exposed separately
    # through VITE_RAZORPAY_KEY_ID for the checkout widget.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    team_plan_amount_paise: int = 30_000
    team_plan_duration_days: int = 30
    # Explorer quota is cumulative indexed source across a user's repositories.
    free_codebase_bytes: int = 200_000_000
    # Team quota is configurable and deliberately finite; do not imply unlimited storage.
    team_codebase_bytes: int = 800_000_000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Repository limits are *budgets*, not failure thresholds. Files are ranked
    # by importance (source and schema first; tests, fixtures, docs, and data
    # later) and the most important files are indexed until a budget is spent.
    # Everything omitted is reported in the coverage panel with its reason.
    max_repository_files: int = 10_000
    max_repository_bytes: int = 100_000_000
    # Source files are chunked before embedding, so this is a per-file safety
    # limit rather than a provider input limit.
    max_file_size_bytes: int = 50_000_000
    # Chunk budget for one repository. Selection estimates chunks before
    # chunking and the worker stops gracefully at the budget instead of
    # failing the job.
    max_repository_chunks: int = 15_000
    # Non-English locale catalogues (for example packages/i18n/locales/fr)
    # are large, repetitive, and match English keys in every language, which
    # floods keyword search. English catalogues are always indexed.
    index_translations: bool = False
    # Repository source is streamed from GitHub's archive endpoint (no git
    # binary). These bound the download and its wall-clock time.
    github_codeload_base_url: str = "https://codeload.github.com"
    github_raw_base_url: str = "https://raw.githubusercontent.com"
    archive_max_download_bytes: int = 1_500_000_000
    source_fetch_timeout_seconds: int = 600
    # The coverage report keeps this many omitted paths (counts stay exact).
    excluded_paths_limit: int = 1_500
    # GitHub OAuth & Git Data API integration
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = ""
    github_frontend_origin: str = ""
    # Render exposes the canonical public service URL automatically. It lets
    # GitHub OAuth work safely when the explicit callback variable has not yet
    # been added to a deployment, without guessing from an untrusted Host
    # header.
    render_external_url: str = ""
    github_oauth_state_ttl_seconds: int = 600
    github_token_encryption_key: str = ""
    # Signs short-lived, file-scoped tickets issued only by the editing query
    # flow. A ticket is required before the GitHub file and PR endpoints act.
    editing_ticket_secret: str = ""
    editing_ticket_ttl_seconds: int = 600
    max_github_change_bytes: int = 10_000_000
    github_editor_max_bytes: int = 2_000_000
    # Keep the shared worker and provider capacity fair across signed-in users.
    max_repositories_per_user: int = 30
    max_active_ingestion_jobs_per_user: int = 1
    ingest_requests_per_minute: int = 6
    query_requests_per_minute: int = 12
    max_concurrent_queries_per_user: int = 2
    clone_timeout_seconds: int = 180
    # Retain enough diverse evidence for accurate multi-file answers without
    # turning each question into an excessively large hosted-model request.
    max_context_characters: int = 40_000
    # Editing requests need more source than ordinary answers. The retrieval
    # layer keeps this bounded and prioritises complete target files; the
    # larger budget prevents a valid hunk from being cut at a chunk boundary.
    # About 60k tokens: a 93k-token prompt was measured answering in 7.2 s
    # and finding a needle in the middle, so more large files are shown whole.
    editing_context_characters: int = 240_000
    retrieval_top_k: int = 8
    editing_retrieval_top_k: int = 32
    # At most this many complete files are handed to the code model. Related
    # imports/callers are selected only when they are present in the indexed
    # evidence, so a large repository never becomes a whole-repository prompt.
    editing_max_files: int = 8
    editing_max_chunks_per_file: int = 256
    # Upper bound for a single file the editor reads or creates.
    editing_file_max_characters: int = 60_000
    # Dense reranking is restricted to lexical/path candidates so a question
    # never performs a repository-wide vector sort on Turso.
    dense_candidate_limit: int = 256
    # File-level semantic search ranks file centroids, then reranks chunks
    # inside this many files.
    file_vector_top_files: int = 12
    # A compact directory map helps answers name exact folders.
    repo_map_max_characters: int = 2_400
    # Broad architectural questions need a representative sample of the index
    # when no code-term match exists in the user's wording.
    overview_retrieval_candidates: int = 64
    conversation_history_messages: int = 6
    max_conversation_history_characters: int = 7_200
    max_conversation_message_characters: int = 1_800
    ingestion_job_timeout_seconds: int = 900
    max_ingestion_attempts: int = 3
    # Run the durable worker in-process so jobs do not remain queued indefinitely.
    local_ingestion_worker: bool = True
    local_ingestion_poll_seconds: float = 3.0
    # Render's free tier sleeps after 15 idle minutes, which would freeze a
    # long indexing job if the user closes the tab. While a job is running the
    # worker pings the public health endpoint so the instance stays awake;
    # once the queue is empty the service can sleep normally.
    keepalive_during_ingestion: bool = True
    keepalive_interval_seconds: int = 240
    public_app_url: str = ""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()


class ModelConfigurationError(RuntimeError):
    """Raised only when a request needs an unavailable LLM configuration."""


def require_nvidia_api_key() -> str:
    key = settings.nvidia_api_key.strip()
    if not key:
        raise ModelConfigurationError(
            "NVIDIA is not configured. Set NVIDIA_API_KEY in the project root .env file and retry."
        )
    return key


def get_cors_origins() -> list[str]:
    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    if "*" in origins:
        raise ValueError("CORS_ORIGINS must list explicit origins when credentials are enabled.")
    return origins


def should_run_local_ingestion_worker() -> bool:
    """Return whether this service instance should process queued ingestion jobs."""
    return settings.local_ingestion_worker


def get_editing_ticket_secret() -> str:
    """Return the dedicated ticket key, with GitHub's server key as fallback.

    Existing deployments already set GITHUB_TOKEN_ENCRYPTION_KEY for the PR
    integration. Reusing it only for HMAC signing keeps the new editing mode
    functional during a rolling deploy; EDITING_TICKET_SECRET can be set to a
    separate value when operators want independent key rotation.
    """
    return settings.editing_ticket_secret.strip() or settings.github_token_encryption_key.strip()


# Model IDs NVIDIA no longer serves to this account (listed by /models, but
# every call returns 404 "Function not found"). Deployments whose environment
# still names one keep working: the request goes to the current model.
RETIRED_CHAT_MODELS = {
    "nvidia/nemotron-3-super-120b-a12b": "nvidia/nemotron-3.5-super-vl-120b-a12b",
}


def _current_chat_model(model_id: str) -> str:
    return RETIRED_CHAT_MODELS.get(model_id.strip(), model_id)


def get_answer_model_options(profile: str, workflow: str = "general") -> tuple[str, bool, int]:
    """Resolve an allow-listed chat profile without accepting model IDs from clients."""
    if workflow == "editing" or profile == "code":
        # Older Render environments may still carry the previously configured
        # Qwen/Codestral IDs. They are not served for every NVIDIA account;
        # transparently use the confirmed Nemotron IDs until those variables
        # are updated in the dashboard and the service is redeployed.
        unavailable_ids = {
            "qwen/qwen2.5-coder-32b-instruct",
            "qwen/qwen3-next-80b-a3b-instruct",
            "mistralai/codestral-22b-instruct-v0.1",
            "deepseek-ai/deepseek-coder-6.7b-instruct",
        }
        code_model = (settings.code_editing_model if settings.code_editing_model not in unavailable_ids
                      else Settings.model_fields["code_editing_model"].default)
        return (
            _current_chat_model(code_model),
            settings.code_editing_enable_thinking,
            settings.code_editing_answer_max_tokens,
        )
    if profile == "detailed":
        return (
            _current_chat_model(settings.detailed_nemotron_model),
            settings.detailed_nvidia_enable_thinking,
            settings.detailed_answer_max_tokens,
        )
    return _current_chat_model(settings.nemotron_model), settings.nvidia_enable_thinking, settings.answer_max_tokens


def get_code_editing_fallback_model() -> str:
    """The editing fallback model (retired IDs remapped), or "" when none is configured."""
    fallback = (settings.code_editing_fallback_model or "").strip()
    return _current_chat_model(fallback) if fallback else ""


def get_alternate_chat_model(profile: str) -> str:
    """The other configured chat model for a question (fast <-> detailed), or "" when it is the same.

    A question whose model fails is tried once on the other configured chat
    model before the evidence-only answer is returned.
    """
    primary = get_answer_model_options(profile)[0]
    other = get_answer_model_options("fast" if profile == "detailed" else "detailed")[0]
    return other if other and other != primary else ""


class RateLimiter:
    def __init__(self, calls_per_minute: int):
        self.calls_per_minute = calls_per_minute
        self.calls: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Acquire without holding the lock while waiting for a slot."""
        while True:
            async with self._lock:
                now = time.time()
                self.calls = [call for call in self.calls if now - call < 60]
                if len(self.calls) < self.calls_per_minute:
                    self.calls.append(now)
                    return
                sleep_time = max(0.1, 60 - (now - self.calls[0]))
            await asyncio.sleep(sleep_time)


nvidia_rate_limiter = RateLimiter(calls_per_minute=settings.nvidia_calls_per_minute)


class UserRequestLimiter:
    """Small process-local fairness guard for authenticated API work.

    Durable per-account quotas still live in Supabase. This limiter protects a
    single web process from bursts before expensive provider work begins.
    """

    def __init__(self, requests_per_minute: int, max_concurrent: int = 1):
        self.requests_per_minute = requests_per_minute
        self.max_concurrent = max_concurrent
        self._requests: dict[str, list[float]] = {}
        self._active: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: str) -> None:
        async with self._lock:
            now = time.monotonic()
            requests = [value for value in self._requests.get(user_id, []) if now - value < 60]
            if len(requests) >= self.requests_per_minute:
                raise RuntimeError("rate_limit")
            if self._active.get(user_id, 0) >= self.max_concurrent:
                raise RuntimeError("concurrency_limit")
            requests.append(now)
            self._requests[user_id] = requests
            self._active[user_id] = self._active.get(user_id, 0) + 1

    async def release(self, user_id: str) -> None:
        async with self._lock:
            active = max(0, self._active.get(user_id, 0) - 1)
            if active:
                self._active[user_id] = active
            else:
                self._active.pop(user_id, None)


ingest_request_limiter = UserRequestLimiter(settings.ingest_requests_per_minute)
query_request_limiter = UserRequestLimiter(
    settings.query_requests_per_minute,
    settings.max_concurrent_queries_per_user,
)
