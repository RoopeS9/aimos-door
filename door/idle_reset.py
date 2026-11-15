"""Restore the shared demo after a quiet period, once per idle period."""
from __future__ import annotations

import math
import os
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware

from door.service import isotime


class DemoIdleResetMiddleware(BaseHTTPMiddleware):
    """Count door requests, excluding probes and the static console files."""

    def __init__(self, app, *, clock=time.monotonic):
        super().__init__(app)
        minutes = float(os.getenv("DEMO_IDLE_RESET_MINUTES", "20"))
        if not math.isfinite(minutes) or minutes < 0:
            raise ValueError("DEMO_IDLE_RESET_MINUTES must be a finite nonnegative number.")
        self.timeout = minutes * 60
        self.clock = clock
        self.last_activity = self.clock()
        self.lock = threading.Lock()

    @staticmethod
    def excluded(path: str) -> bool:
        # The console HTML and hashed bundles are static too: loading or polling
        # them must neither wake the demo nor trigger a state-changing reset.
        return path in {"/healthz", "/", "/assets"} or path.startswith("/assets/")

    async def dispatch(self, request, call_next):
        if self.timeout == 0 or self.excluded(request.url.path):
            return await call_next(request)

        reset = False
        # Keep the reset inside the claim lock. Concurrent arrivals wait for the
        # complete reset, then see fresh activity instead of resetting again.
        with self.lock:
            now = self.clock()
            if now - self.last_activity >= self.timeout:
                request.app.state.service.reset()
                request.app.state.service.idle_reset_at = isotime()
                reset = True
            self.last_activity = now

        response = await call_next(request)
        if reset:
            response.headers["X-Demo-Idle-Reset"] = "1"
        return response
