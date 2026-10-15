"""Run the full backend locally against a local libSQL database.

    backend/venv/bin/python scripts/dev_server.py --insecure-local-auth

* Uses a local database (default: a sqld server at http://127.0.0.1:18090 if
  one is running, otherwise an embedded file under .local/) so development
  never touches a production Turso database. Override with --database.
* --insecure-local-auth replaces Supabase sign-in with one fixed local user so
  the UI can be exercised without Google OAuth. It only works when the server
  is bound to 127.0.0.1 and every request comes from the loopback interface.
  Never use this for anything reachable from a network.

The frontend can then be started with
    VITE_API_BASE_URL=http://127.0.0.1:8000/api npm --prefix frontend run dev
and, with --insecure-local-auth, a browser session token is not required by
the API (any bearer value is accepted from loopback).
"""

import argparse
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def _sqld_running(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", help="libSQL URL (http://127.0.0.1:PORT for sqld, or file:path)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--insecure-local-auth", action="store_true", help="accept a fixed local user from loopback only")
    parser.add_argument("--local-user-id", default="local-dev-user", help="user id for --insecure-local-auth (default: local-dev-user)")
    parser.add_argument("--cors-origins", default="http://localhost:5173,http://127.0.0.1:5173", help="comma-separated browser origins allowed by CORS")
    parser.add_argument("--no-worker", action="store_true", help="do not run the ingestion worker in-process")
    args = parser.parse_args()

    database = args.database
    if not database:
        if _sqld_running("127.0.0.1", 18090):
            database = "http://127.0.0.1:18090"
        else:
            local = ROOT / ".local"
            local.mkdir(exist_ok=True)
            database = f"file:{local / 'dev.db'}"
    if not (database.startswith("file:") or "127.0.0.1" in database or "localhost" in database):
        print("Refusing to run the dev server against a non-local database.", file=sys.stderr)
        return 2

    os.environ["TURSO_DATABASE_URL"] = database
    os.environ["TURSO_AUTH_TOKEN"] = os.environ.get("DEV_TURSO_AUTH_TOKEN", "")
    os.environ["KEEPALIVE_DURING_INGESTION"] = "false"
    os.environ["CORS_ORIGINS"] = args.cors_origins
    os.environ["GITHUB_FRONTEND_ORIGIN"] = "http://localhost:5173"
    if args.no_worker:
        os.environ["LOCAL_INGESTION_WORKER"] = "false"
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)

    import uvicorn
    from fastapi import HTTPException, Request

    from api.auth import AuthenticatedUser, get_current_user
    from main import app

    if args.insecure_local_auth:
        def local_user(request: Request) -> AuthenticatedUser:
            client = request.client.host if request.client else ""
            if client not in {"127.0.0.1", "::1"}:
                raise HTTPException(status_code=401, detail="Local auth only accepts loopback requests.")
            return AuthenticatedUser(id=args.local_user_id, access_token="local", raw_user=None)

        app.dependency_overrides[get_current_user] = local_user
        print("WARNING: insecure local auth is enabled (loopback only).", file=sys.stderr)

    print(f"Backend: http://127.0.0.1:{args.port}/api  (database: {database})")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
