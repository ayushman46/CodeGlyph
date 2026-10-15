import re
import threading
from array import array
import time
from collections import deque
from functools import lru_cache
from typing import Callable, Dict, List

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from config import require_nvidia_api_key, settings

EMBEDDING_DIMENSION = settings.embedding_dimension
_client = None
_request_times: deque[tuple[float, str]] = deque()
_rate_limit_lock = threading.Lock()


class EmbeddingUnavailableError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def is_rejected_input(error: EmbeddingUnavailableError) -> bool:
    """A 4xx (other than 429) means this input will never be accepted."""
    code = getattr(error, "status_code", None)
    return code is not None and 400 <= code < 500 and code != 429


# NVIDIA's multimodal-aware embedding endpoint parses any "data:image/..."
# substring as an image input and rejects the whole request with HTTP 400
# ("image inputs require VLM serving"). Source code legitimately contains
# such strings (SSRF guards, CSS, tests), so break the data-URI shape before
# embedding. The stored chunk text used for search and citations is unchanged.
_DATA_URI = re.compile(r"\b(data):(?=\s*(?:image|video|audio)/)", re.IGNORECASE)


def sanitize_embedding_text(text: str) -> str:
    return _DATA_URI.sub(r"\1 :", text)


# Interactive questions must never stall behind the shared limiter: when no
# slot frees up within this many seconds, the query embedding is skipped and
# retrieval continues with keyword and path evidence.
QUERY_SLOT_MAX_WAIT_SECONDS = 1.0
# A query embedding is a single short request. One attempt with a short client
# timeout keeps the keyword fallback fast when NVIDIA is slow or failing.
QUERY_EMBEDDING_CLIENT_TIMEOUT_SECONDS = 4.0


def wait_for_embedding_slot(input_type: str, max_wait_seconds: float | None = None):
    """Throttle embeddings while reserving capacity for live repository questions.

    ``max_wait_seconds`` bounds the wait (used for interactive queries); when
    no slot frees up in time ``EmbeddingUnavailableError`` is raised instead
    of sleeping for up to a minute.
    """
    calls_per_minute = max(1, settings.nvidia_calls_per_minute)
    is_query = input_type == "query"
    passage_limit = max(1, calls_per_minute - max(0, settings.query_embedding_reserve_per_minute))
    deadline = None if max_wait_seconds is None else time.monotonic() + max(0.0, float(max_wait_seconds))
    while True:
        with _rate_limit_lock:
            now = time.monotonic()
            while _request_times and now - _request_times[0][0] >= 60:
                _request_times.popleft()
            recent_calls = len(_request_times)
            if recent_calls < (calls_per_minute if is_query else passage_limit):
                _request_times.append((now, input_type))
                return
            wait_seconds = max(0.1, 60 - (now - _request_times[0][0]))
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or wait_seconds > remaining:
                raise EmbeddingUnavailableError("The embedding rate limit is saturated; using keyword retrieval.", status_code=429)
            wait_seconds = min(wait_seconds, remaining)
        time.sleep(wait_seconds)


def should_retry_embedding_error(error: Exception) -> bool:
    if isinstance(error, (APITimeoutError, APIConnectionError)):
        return True
    return isinstance(error, APIStatusError) and error.status_code in {429, 500, 502, 503, 504}


def embedding_retry_delay(error: Exception, attempt: int) -> float:
    """Prefer the provider's cooldown hint, otherwise use bounded backoff."""
    response = getattr(error, "response", None)
    retry_after = response.headers.get("retry-after") if response is not None else None
    try:
        if retry_after is not None:
            return min(60.0, max(0.1, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return min(30.0, settings.embedding_retry_base_seconds * (2 ** attempt))


def get_embedding_client():
    """Return the hosted NVIDIA embedding client."""
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.nvidia_base_url,
            api_key=require_nvidia_api_key(),
            timeout=settings.nvidia_timeout_seconds,
            max_retries=0,
        )
    return _client


def embed_texts(texts: List[str], *, input_type: str) -> List[List[float]]:
    """Generate NVIDIA embeddings, keeping indexing and search modes explicit."""
    if not texts:
        return []
    last_error: Exception | None = None
    is_query = input_type == "query"
    if is_query:
        # Check configuration before taking a limiter slot: without a key a
        # question must fall back to keyword retrieval immediately.
        require_nvidia_api_key()
    attempts = 1 if is_query else max(1, settings.embedding_retry_attempts)
    for attempt in range(attempts):
        try:
            wait_for_embedding_slot(input_type, QUERY_SLOT_MAX_WAIT_SECONDS if is_query else None)
            embedding_model = settings.embedding_model
            # The retired E5/NVIDIA model was previously used by this project.
            # Keep old Render environment values from breaking re-indexing;
            # the current 2048-dimensional model is the safe replacement.
            if embedding_model in {"nvidia/nv-embedqa-e5-v5", "nvidia/nv-embedqa-e5-v5:latest"}:
                embedding_model = "nvidia/nemotron-3-embed-1b"
            client = get_embedding_client()
            if is_query:
                client = client.with_options(timeout=QUERY_EMBEDDING_CLIENT_TIMEOUT_SECONDS)
            response = client.embeddings.create(
                model=embedding_model,
                input=[sanitize_embedding_text(text) for text in texts],
                extra_body={"input_type": input_type, "truncate": "END"},
            )
            vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
            last_error = None
            break
        except Exception as error:
            last_error = error
            if not should_retry_embedding_error(error) or attempt == attempts - 1:
                break
            time.sleep(embedding_retry_delay(error, attempt))
    else:  # pragma: no cover - the loop always breaks or returns a response.
        last_error = RuntimeError("Embedding retry loop ended unexpectedly.")

    if last_error is not None:
        if isinstance(last_error, EmbeddingUnavailableError):
            raise last_error
        if isinstance(last_error, APITimeoutError):
            raise EmbeddingUnavailableError("NVIDIA timed out while creating repository embeddings after retries.") from last_error
        if isinstance(last_error, APIConnectionError):
            raise EmbeddingUnavailableError("Could not connect to NVIDIA for repository embeddings after retries.") from last_error
        if isinstance(last_error, APIStatusError):
            raise EmbeddingUnavailableError(
                f"NVIDIA could not create repository embeddings after retries (HTTP {last_error.status_code}).",
                status_code=last_error.status_code,
            ) from last_error
        raise EmbeddingUnavailableError("NVIDIA returned an invalid embedding response.") from last_error

    if len(vectors) != len(texts) or any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
        raise EmbeddingUnavailableError(
            f"NVIDIA returned embeddings incompatible with the expected {EMBEDDING_DIMENSION}-dimension schema."
        )
    return vectors


@lru_cache(maxsize=128)
def embed_query(query: str) -> List[float]:
    """Cache repeated interactive searches to avoid a second hosted request."""
    return embed_texts([query], input_type="query")[0]

def embed_chunks(
    chunks: List[Dict],
    *,
    initial_batch_size: int | None = None,
    on_batch_size_change: Callable[[int], None] | None = None,
    on_request: Callable[[], None] | None = None,
) -> List[Dict]:
    if not chunks:
        return chunks

    # NVIDIA's hosted endpoint is most reliable when code-heavy requests stay
    # bounded. Start with the configured batch and split only a rejected
    # payload, so normal indexing uses fewer provider round trips without
    # dropping or changing any chunk.
    batch_size = max(1, initial_batch_size or settings.embedding_batch_size)
    minimum_batch_size = min(batch_size, max(1, settings.embedding_min_batch_size))
    max_batch_characters = max(1, int(settings.embedding_batch_max_characters))
    offset = 0
    while offset < len(chunks):
        current_size = min(batch_size, len(chunks) - offset)
        # A count-only batch can be unexpectedly large when a repository has
        # long code chunks. Bound payload characters as well as passage count;
        # this keeps latency predictable and avoids a provider rejection that
        # would otherwise cost a full retry before adaptive splitting runs.
        payload_characters = 0
        bounded_size = 0
        for candidate in chunks[offset:offset + current_size]:
            candidate_size = len(candidate.get("content", "")) + len(candidate.get("file_path", "")) + 18
            if bounded_size and payload_characters + candidate_size > max_batch_characters:
                break
            payload_characters += candidate_size
            bounded_size += 1
        current_size = max(1, bounded_size)
        batch = chunks[offset:offset + current_size]
        texts_to_embed = [
            f"File: {c['file_path']}\nContent:\n{c['content']}"
            for c in batch
        ]
        try:
            if on_request is not None:
                on_request()
            embeddings = embed_texts(texts_to_embed, input_type="passage")
        except EmbeddingUnavailableError as error:
            if current_size <= minimum_batch_size:
                if not is_rejected_input(error):
                    raise
                # A permanent rejection at the smallest batch: embed these
                # passages one at a time and skip only the ones the provider
                # refuses, so one odd file never disables semantic search for
                # the whole repository. Skipped chunks stay keyword-searchable.
                for chunk, text in zip(batch, texts_to_embed):
                    try:
                        if on_request is not None:
                            on_request()
                        chunk['embedding'] = array('f', embed_texts([text], input_type="passage")[0])
                    except EmbeddingUnavailableError as single_error:
                        if not is_rejected_input(single_error):
                            raise
                        chunk['_embedding_skipped'] = True
                offset += current_size
                continue
            # A provider can reject a large payload even while accepting the
            # same passages individually. Split only the failing batch; all
            # vectors are still generated by the same model and validated.
            batch_size = max(minimum_batch_size, current_size // 2)
            if on_batch_size_change is not None:
                on_batch_size_change(batch_size)
            continue

        for chunk, emb in zip(batch, embeddings):
            # Keep vectors packed while they move through the worker. A Python
            # list of 2,048 float objects can leave hundreds of MB in the
            # allocator after a large repository, even though only one bounded
            # batch is live at a time. Turso's vector32 column has the same
            # float32 precision, so this is lossless for persisted/search data.
            chunk['embedding'] = array('f', emb)
        offset += current_size

    return chunks
