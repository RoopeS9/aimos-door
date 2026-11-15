"""Optional demo gate and bounded per-client HTTP admission."""
from __future__ import annotations

from collections import defaultdict, deque
import hmac
import ipaddress
import os
import secrets
import time
from urllib.parse import parse_qs

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


LOGIN_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aimo's Door — demo access</title><style>body{background:#F6F4EE;color:#14253B;font:18px system-ui;margin:0;display:grid;place-items:center;min-height:100vh}main{max-width:420px;padding:40px}label,input,button{display:block;margin:16px 0}input{padding:12px;font:inherit;border:1px solid #14253B;border-radius:6px;width:90%}button{background:#1F6F5C;color:white;border:0;padding:14px 24px;border-radius:6px;font:inherit}</style><main><h1>Aimo's Door</h1><p>Enter the access code to open this demo.</p><form action="/demo/unlock" method="post"><label for="access_code">Demo access code</label><input id="access_code" name="access_code" type="password" autocomplete="current-password" required><button type="submit">Open demo</button></form></main></html>"""


class DemoProtection(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.code = os.getenv("DEMO_ACCESS_CODE", "")
        self.session = secrets.token_urlsafe(48)
        self.rate = max(1, int(os.getenv("DEMO_RATE_LIMIT", "120")))
        self.window = max(1, int(os.getenv("DEMO_RATE_WINDOW_SECONDS", "60")))
        self.bot_rate = max(1, int(os.getenv("BOT_CHAT_RATE_LIMIT", "20")))
        self.bot_window = max(1, int(os.getenv("BOT_CHAT_RATE_WINDOW_SECONDS", "60")))
        self.fail_limit = max(1, int(os.getenv("DEMO_ACCESS_MAX_FAILURES", "10")))
        self.fail_window = max(1, int(os.getenv("DEMO_ACCESS_LOCKOUT_SECONDS", "600")))
        self.failures: dict[str, deque] = defaultdict(deque)
        self.requests: dict[str, deque] = defaultdict(deque)
        self.bot_requests: dict[str, deque] = defaultdict(deque)

    @staticmethod
    def client_address(request):
        peer = request.client.host if request.client else "unknown"
        try:
            address = ipaddress.ip_address(peer)
            trusted = address.is_loopback or any(address in ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
        except ValueError:
            trusted = False
        # Opt-in: a header set by the edge in front of the proxy (for example Cf-Connecting-Ip),
        # honoured only when the request comes from a private or loopback peer.
        edge_header = os.getenv("CLIENT_IP_HEADER", "").strip()
        if trusted and edge_header:
            try:
                return str(ipaddress.ip_address(request.headers.get(edge_header, "").strip()))
            except ValueError:
                pass
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if trusted and forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        # Uvicorn may already have resolved a trusted proxy's forwarded address.
        return peer

    @staticmethod
    def _allow(buckets, address, now, window, limit):
        """Admit into one bounded address bucket, returning its retry delay."""
        if address not in buckets and len(buckets) >= 4096:
            stale = [key for key, values in buckets.items() if not values or values[-1] <= now - window]
            for key in stale:
                del buckets[key]
            if len(buckets) >= 4096:
                return window
        recent = buckets[address]
        while recent and recent[0] <= now - window:
            recent.popleft()
        if len(recent) >= limit:
            return window
        recent.append(now)
        return None

    def _locked(self, address, now):
        """Seconds left in this address's lockout, or None when it may try a code."""
        recent = self.failures.get(address)
        if not recent:
            return None
        while recent and recent[0] <= now - self.fail_window:
            recent.popleft()
        if not recent:
            del self.failures[address]
            return None
        if len(recent) >= self.fail_limit:
            return max(1, int(recent[0] + self.fail_window - now) + 1)
        return None

    def _record_failure(self, address, now):
        if address not in self.failures and len(self.failures) >= 4096:
            for key in [key for key, values in self.failures.items() if not values or values[-1] <= now - self.fail_window]:
                del self.failures[key]
            if len(self.failures) >= 4096:
                return
        self.failures[address].append(now)

    @staticmethod
    def _lockout_response(retry):
        return JSONResponse({"detail": "Too many wrong access codes from this address. Try again later."}, status_code=429, headers={"Retry-After": str(retry), "Cache-Control": "no-store"})

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/healthz" or path.startswith("/assets/"):
            return await call_next(request)
        now = time.monotonic()
        address = self.client_address(request)
        internal_token = getattr(request.app.state, "bot_internal_token", "")
        internal = bool(
            path != "/bot/chat"
            and internal_token
            and hmac.compare_digest(request.headers.get("x-door-internal-bot", ""), internal_token)
        )
        if path == "/bot/chat":
            retry = self._allow(self.bot_requests, address, now, self.bot_window, self.bot_rate)
            if retry is not None:
                return JSONResponse({"detail": "The per-address bot chat limit has been reached. Try again shortly."}, status_code=429, headers={"Retry-After": str(retry)})
        if not internal:
            retry = self._allow(self.requests, address, now, self.window, self.rate)
            if retry is not None:
                return JSONResponse({"detail": "The per-IP demo request limit has been reached. Try again shortly."}, status_code=429, headers={"Retry-After": str(retry)})
        if path == "/demo/unlock" and request.method == "POST":
            raw = await request.body()
            if len(raw) > 2048:
                return JSONResponse({"detail": "Access request is too large."}, status_code=413)
            provided = parse_qs(raw.decode("utf-8", errors="replace")).get("access_code", [""])[0]
            if self.code:
                retry = self._locked(address, now)
                if retry is not None:
                    return self._lockout_response(retry)
                if not hmac.compare_digest(provided.encode(), self.code.encode()):
                    self._record_failure(address, now)
                    return HTMLResponse(LOGIN_PAGE, status_code=403, headers={"Cache-Control": "no-store"})
                self.failures.pop(address, None)
            response = RedirectResponse("/", status_code=303)
            secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie("demo_access", self.session, httponly=True, secure=secure, samesite="strict", max_age=8 * 3600)
            return response
        public = path in {"/.well-known/jwks.json", "/catalog/search", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
        header = request.headers.get("x-demo-access-code", "")
        cookie = request.cookies.get("demo_access", "")
        by_cookie = hmac.compare_digest(cookie, self.session)
        by_header = False
        if self.code and header and not by_cookie:
            # A code sent as a header is a guess like any other: same lockout as the unlock page.
            retry = self._locked(address, now)
            if retry is not None:
                return self._lockout_response(retry)
            by_header = hmac.compare_digest(header.encode(), self.code.encode())
            if not by_header:
                self._record_failure(address, now)
        authorized = by_cookie or by_header
        if self.code and not public and not authorized:
            if path == "/":
                return HTMLResponse(LOGIN_PAGE, status_code=401, headers={"Cache-Control": "no-store"})
            return JSONResponse({"detail": "The demo access code is required."}, status_code=401)
        return await call_next(request)
