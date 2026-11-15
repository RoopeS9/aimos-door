"""Offline tests cannot accidentally spend model credit or hit the Valio API."""
from urllib.parse import urlsplit

import httpx
import pytest


@pytest.fixture(autouse=True)
def prohibit_external_network_in_offline_tests(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return
    original_sync = httpx.Client.send
    original_async = httpx.AsyncClient.send

    def check(request):
        host = urlsplit(str(request.url)).hostname
        assert host in {"127.0.0.1", "localhost", "testserver", "test", "door"}, "Offline tests may not call external services; use fixtures or mark the exact acceptance case LIVE."

    def sync(self, request, *args, **kwargs):
        if not isinstance(self._transport, httpx.MockTransport):
            check(request)
        return original_sync(self, request, *args, **kwargs)

    async def asynchronous(self, request, *args, **kwargs):
        if not isinstance(self._transport, httpx.MockTransport):
            check(request)
        return await original_async(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", sync)
    monkeypatch.setattr(httpx.AsyncClient, "send", asynchronous)
