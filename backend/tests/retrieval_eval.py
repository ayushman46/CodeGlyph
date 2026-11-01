"""Deterministic retrieval evaluation built from real customer questions.

    cd backend && venv/bin/python tests/retrieval_eval.py --database file:/path/to/copy.db

Each case names a repository by (user id, repository name), a question, the
workflow it was asked in, and the file/line targets a correct answer needs.
The question runs through ``retrieve_context`` with semantic search disabled
(keyword, symbol, path and dependency evidence only), so the result does not
depend on an embedding provider. The evidence is then rendered with the
production context budget, and a target counts as found only when it is
among the sources the answer model would actually see.

The database must be a local ``file:`` copy; the script refuses anything else
and never reads the project ``.env`` credentials. It is not a unit test (the
file name does not start with ``test``) because it needs an indexed database.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# (target path, line) means a shown source of that file covers the line;
# (target path, None) means any section of that file is shown. ``avoid`` is a
# path regex with the most matching sources that are acceptable.
CASES = [
    {
        "id": "BUG-04 httpx signature change",
        "user": "sim-dev", "repo": "httpx", "workflow": "general",
        "question": "If I change the signature of Client._send_single_request in httpx/_client.py, which modules "
                    "import _client.py and what else would be affected?",
        "expect": [("httpx/_client.py", 1001), ("httpx/_client.py", 979), ("httpx/_client.py", 1694)],
    },
    {
        "id": "BUG-04 flask callers of make_response",
        "user": "sim-lead", "repo": "flask", "workflow": "general",
        "question": "Who calls Flask.make_response in src/flask/app.py?",
        "expect": [("src/flask/app.py", 1267), ("src/flask/app.py", 1078)],
    },
    {
        "id": "BUG-04 flask blast radius",
        "user": "sim-lead", "repo": "flask", "workflow": "general",
        "question": "We plan to change how Flask.make_response in src/flask/app.py converts view return values "
                    "(tuples, dicts, strings, None) into Response objects. What is the blast radius? List every caller "
                    "of Flask.make_response, the public APIs that route through it (including flask.make_response in "
                    "helpers.py), and the test files that exercise this behaviour, with file:line citations. Separate "
                    "direct callers from indirect dependents.",
        "expect": [("src/flask/app.py", 1267), ("src/flask/app.py", 1078), ("src/flask/helpers.py", 197)],
    },
    {
        "id": "BUG-04 express verb methods in a named file",
        "user": "sim-onboard", "repo": "express", "workflow": "architecture",
        "question": "Where does lib/application.js define app.get and the other HTTP verb methods, and what do they register?",
        "expect": [("lib/application.js", 471)],
    },
    {
        "id": "BUG-06 express tests for res.redirect",
        "user": "sim-onboard", "repo": "express", "workflow": "onboarding",
        "question": "If I fix a small bug in res.redirect or res.location in lib/response.js, which test files cover that code?",
        "expect": [("test/res.redirect.js", None), ("test/res.location.js", None), ("lib/response.js", 815)],
    },
    {
        "id": "BUG-06 requests _encode_files",
        "user": "sim-maint", "repo": "requests", "workflow": "general",
        "question": 'Bug report: requests.Request("POST", "http://x", data={1: "a"}).prepare() works, but '
                    'requests.Request("POST", "http://x", data={1: "a"}, files={"f": b"x"}).prepare() raises '
                    "AttributeError: 'int' object has no attribute 'translate' from inside urllib3. files={1: b\"x\"} "
                    "fails the same way. Why? Walk me through the code path from prepare() to where it breaks, with citations.",
        "expect": [("src/requests/models.py", 183), ("src/requests/models.py", 245), ("src/requests/models.py", 637)],
    },
    {
        "id": "BUG-06 httpx Client.get call path",
        "user": "sim-dev", "repo": "httpx", "workflow": "general",
        "question": "Trace the call path from httpx.Client.get down to the point where the request bytes are actually "
                    "sent over the network. List each function in order with its file and line.",
        "expect": [("httpx/_client.py", 1036), ("httpx/_client.py", 1014)],
    },
    {
        "id": "BUG-06 click Command.invoke body",
        "user": "sim-first", "repo": "click", "workflow": "general",
        "question": "After the parser collects the raw strings, where does click apply defaults and environment "
                    "variables, convert values with the parameter type, and finally call my decorated function with them?",
        "expect": [("src/click/core.py", 1446), ("src/click/core.py", 2824), ("src/click/core.py", 2704)],
    },
    {
        "id": "BUG-06 express tests and lint question",
        "user": "sim-onboard", "repo": "express", "workflow": "onboarding",
        "question": "How are the tests organized and named, how do I run a single test file locally, and what lint or "
                    "style rules does the project enforce? Keep it short.",
        "expect": [("package.json", None)],
        "avoid": (r"\.css$", 1),
    },
    {
        "id": "BUG-06 httpx timeout outside tests and docs",
        "user": "sim-dev", "repo": "httpx", "workflow": "general",
        "question": "Find every place in the library code (not tests or docs) that reads or applies the timeout "
                    "configuration, from the Client constructor to the transport. List each with file:line.",
        "expect": [("httpx/_client.py", 584), ("httpx/_client.py", 910), ("httpx/_config.py", 88)],
        "avoid": (r"(?:^|/)(?:tests?|docs)/|\.md$", 0),
    },
    {
        "id": "httpx _set_timeout call sites",
        "user": "sim-dev", "repo": "httpx", "workflow": "general",
        "question": "List every call site of _set_timeout and show its definition.",
        "expect": [("httpx/_client.py", 584), ("httpx/_client.py", 910), ("httpx/_client.py", 1625)],
    },
    {
        "id": "httpx where is Client.get",
        "user": "sim-dev", "repo": "httpx", "workflow": "general",
        "question": "Where is Client.get defined?",
        "expect": [("httpx/_client.py", 1036)],
    },
    {
        "id": "click process_value usages",
        "user": "sim-first", "repo": "click", "workflow": "general",
        "question": "Where is process_value used, and what calls it?",
        "expect": [("src/click/core.py", 2802), ("src/click/core.py", 2977)],
    },
    {
        "id": "control: click custom ParamType",
        "user": "sim-first", "repo": "click", "workflow": "general",
        "question": "How do I write a custom parameter type in click? Which base class do I subclass, which methods "
                    "must I implement, and how should I report an invalid value? Point me to an existing type in the "
                    "code I can copy.",
        "expect": [("src/click/types.py", 65), ("src/click/types.py", 215)],
    },
    {
        "id": "control: ky retry decision",
        "user": "baseline", "repo": "ky", "workflow": "general",
        "question": "How does ky decide whether to retry a failed request?",
        "expect": [("source/core/Ky.ts", 708)],
    },
]


def _covers(source: dict, path: str, line: int | None) -> bool:
    if str(source.get("file_path")) != path:
        return False
    if line is None:
        return True
    return int(source.get("start_line") or 0) <= line <= int(source.get("end_line") or 0)


async def evaluate(database: str, only: str | None = None) -> dict:
    from unittest.mock import patch

    from api.query_router import build_context_with_coverage
    from config import settings
    from database import TursoStore
    from ingest.embedder import EmbeddingUnavailableError
    from retrieval import retriever

    store = TursoStore(database, "")
    results = []
    with patch.object(retriever, "embed_query", side_effect=EmbeddingUnavailableError("evaluation runs keyword-only")):
        for case in CASES:
            if only and only.lower() not in case["id"].lower():
                continue
            rows = await store.fetch_all(
                "SELECT id FROM repos WHERE user_id = ? AND repo_name = ?", [case["user"], case["repo"]],
            )
            if not rows:
                results.append({"id": case["id"], "error": "repository not found", "found": 0, "total": len(case["expect"])})
                continue
            chunks = await retriever.retrieve_context(
                store, rows[0]["id"], case["question"], top_k=settings.retrieval_top_k, workflow=case["workflow"],
            )
            _, shown = build_context_with_coverage(chunks)
            sources = [
                {"file_path": item.get("file_path"), "start_line": item.get("start_line"), "end_line": item.get("end_line"),
                 "methods": item.get("_retrieval_methods") or []}
                for item in shown
            ]
            hits = [
                {"target": f"{path}:{line}" if line else path, "found": any(_covers(source, path, line) for source in sources)}
                for path, line in case["expect"]
            ]
            avoid_ok = True
            avoid_count = 0
            if case.get("avoid"):
                pattern, allowed = case["avoid"]
                avoid_count = sum(1 for source in sources if re.search(pattern, str(source["file_path"])))
                avoid_ok = avoid_count <= allowed
            results.append({
                "id": case["id"], "found": sum(hit["found"] for hit in hits), "total": len(hits),
                "targets": hits, "avoid_ok": avoid_ok, "avoid_count": avoid_count, "sources": sources,
            })
    found = sum(result["found"] for result in results)
    total = sum(result["total"] for result in results)
    passed = sum(1 for result in results if result.get("found") == result.get("total") and result.get("avoid_ok", True))
    return {"recall": round(found / total, 3) if total else 0.0, "found": found, "total": total,
            "cases_passed": passed, "cases": len(results), "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, help="file:/path/to/a/local/copy.db")
    parser.add_argument("--only", help="run the cases whose id contains this text")
    parser.add_argument("--json", help="write the full result to this file")
    parser.add_argument("--verbose", action="store_true", help="print every source")
    args = parser.parse_args()
    if not args.database.startswith("file:"):
        print("Refusing to evaluate against a non-local database; pass a file: copy.", file=sys.stderr)
        return 2
    # Environment variables take precedence over the project .env file.
    os.environ["TURSO_DATABASE_URL"] = args.database
    os.environ["TURSO_AUTH_TOKEN"] = ""
    os.environ["NVIDIA_API_KEY"] = ""
    os.environ["LOCAL_INGESTION_WORKER"] = "false"
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    import logging
    logging.disable(logging.WARNING)

    report = asyncio.run(evaluate(args.database, args.only))
    for result in report["results"]:
        status = "PASS" if result.get("found") == result.get("total") and result.get("avoid_ok", True) else "MISS"
        missing = [hit["target"] for hit in result.get("targets", []) if not hit["found"]]
        extra = "" if result.get("avoid_ok", True) else f"  avoid-pattern sources: {result['avoid_count']}"
        print(f"{status} {result['found']}/{result['total']}  {result['id']}"
              + (f"  missing: {', '.join(missing)}" if missing else "") + extra)
        if args.verbose:
            for source in result.get("sources", []):
                print(f"      {source['file_path']}:{source['start_line']}-{source['end_line']}  {','.join(source['methods'])}")
    print(f"\nTarget recall {report['found']}/{report['total']} = {report['recall']:.0%}; "
          f"cases fully passing {report['cases_passed']}/{report['cases']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
