"""Caching and compression for the built frontend in single-service deploys.

Vite names every file under ``assets/`` after its content hash, so an asset
URL never changes meaning: browsers may keep it for a year without asking
again. Every other file (``index.html``, ``theme-init.js``,
``workspace-preload.js``, icons) keeps its name across deploys, so it is
revalidated on each load (a cheap 304) and a deploy shows up straight away.

Without these headers browsers fell back to heuristic caching and re-checked
each script on most visits, a round trip each over a far-away origin.

The build writes ``.br`` and ``.gz`` copies of text assets (see
``frontend/vite.config.js``). Serving those spends no CPU per request on a
small instance, and the build's maximum-quality brotli is about a fifth
smaller than compressing on the fly.
"""

from __future__ import annotations

import mimetypes
import os

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import Scope

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
_ENCODINGS = (("br", ".br"), ("gzip", ".gz"))


def accepted_encodings(header: str) -> set[str]:
    """Content codings an Accept-Encoding header allows (``q=0`` refuses one)."""
    accepted: set[str] = set()
    for part in header.split(","):
        name, _, params = part.strip().partition(";")
        name = name.strip().lower()
        if not name:
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality > 0:
            accepted.add(name)
    return accepted


class HashedAssets(StaticFiles):
    """``assets/``: cached for a year, served from precompressed copies when accepted."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        request_headers = Headers(scope=scope)
        accepted = accepted_encodings(request_headers.get("accept-encoding", ""))
        if "*" in accepted:
            accepted |= {"br", "gzip"}
        response: FileResponse | None = None
        media_type = mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"
        for encoding, suffix in _ENCODINGS:
            if encoding not in accepted:
                continue
            compressed = f"{full_path}{suffix}"
            try:
                compressed_stat = os.stat(compressed)
            except OSError:
                continue
            response = FileResponse(compressed, status_code=status_code, stat_result=compressed_stat, media_type=media_type)
            response.headers["Content-Encoding"] = encoding
            break
        if response is None:
            response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        response.headers["Cache-Control"] = IMMUTABLE
        response.headers["Vary"] = "Accept-Encoding"
        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response


def revalidated_file(path: str | os.PathLike[str]) -> FileResponse:
    """A file whose name outlives deploys: the browser checks it on each load."""
    response = FileResponse(path)
    response.headers["Cache-Control"] = REVALIDATE
    return response
