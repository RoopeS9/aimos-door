"""Budgeted model warm-up and shared activity tracking for the live bot."""
from __future__ import annotations

import asyncio
import time

from door.model_budget import ModelLimitError, admit
from reference_bot.providers import ProviderError, ProviderLimitError, ToolProvider


class BotRuntime:
    interval = 300
    idle_timeout = 1800

    def __init__(self, data_dir, *, clock=time.monotonic, provider=None):
        self.data_dir, self.clock = data_dir, clock
        self.started_at = self.clock()
        self.last_activity = float("-inf")
        self.last_warm_attempt = float("-inf")
        self.idle_stopped = False
        self.warm_after_idle = False
        self.status = {"state": "starting"}
        self.provider = provider or ToolProvider()
        self.provider.before_request = self.admit_request

    def admit_request(self):
        admit(self.data_dir, "bot")
        self.last_activity = self.clock()
        if self.idle_stopped:
            # The real request itself warms the provider. Resume background
            # warming immediately too, then apply the same fresh idle window.
            self.idle_stopped = False
            self.warm_after_idle = True

    async def tick(self):
        now = self.clock()
        if self.status["state"] == "blocked":
            return
        idle_since = self.last_activity if self.last_activity != float("-inf") else self.started_at
        if self.warm_after_idle:
            pass
        elif now - idle_since >= self.idle_timeout:
            self.idle_stopped = True
            return
        elif now - max(self.last_activity, self.last_warm_attempt) < self.interval:
            return
        self.warm_after_idle = False
        self.last_warm_attempt = now
        try:
            model = await self.provider.warmup()
            self.status = {"state": "ready", "model": model}
        except (ModelLimitError, ProviderLimitError):
            self.status = {"state": "blocked", "reason": "usage_or_credit_limit"}
        except ProviderError:
            self.status = {"state": "unavailable", "reason": "Warm-up failed; a user request still has its bounded recovery attempts."}

    async def run(self):
        # tick() performs the initial call immediately; subsequent ticks are
        # gated by both the last warm attempt and real bot activity.
        while self.status["state"] != "blocked":
            await self.tick()
            await asyncio.sleep(self.interval)
