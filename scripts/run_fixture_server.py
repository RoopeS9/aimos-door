#!/usr/bin/env python3
"""Serve the actual app and built console with offline upstream fixtures only."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    # dotenv never replaces these explicit empty values. Headless acceptance
    # must not be able to spend credit even if provider startup behavior changes.
    for name in ("VALIO_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "AGENT_API_KEY", "DEMO_ACCESS_CODE", "DOOR_SIGNING_KEY"):
        os.environ[name] = ""
    os.environ.update(AGENT_WARMUP_ENABLED="0", DEMO_RATE_LIMIT="10000", PUBLIC_BASE_URL=f"http://127.0.0.1:{args.port}", DOOR_API_BASE_URL=f"http://127.0.0.1:{args.port}")
    import httpx
    import uvicorn
    from door.main import create_app

    original_sync, original_async = httpx.Client.send, httpx.AsyncClient.send

    def check(request):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("The headless fixture server forbids all external HTTP calls.")

    def synchronous(self, request, *values, **kwargs):
        check(request)
        return original_sync(self, request, *values, **kwargs)

    async def asynchronous(self, request, *values, **kwargs):
        check(request)
        return await original_async(self, request, *values, **kwargs)

    httpx.Client.send, httpx.AsyncClient.send = synchronous, asynchronous
    with tempfile.TemporaryDirectory(prefix="aimos-headless-") as folder:
        application = create_app(Path(folder), fixture=True)
        uvicorn.run(application, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
