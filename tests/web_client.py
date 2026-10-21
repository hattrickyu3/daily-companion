"""Webのテストで使う TestClient. 本人の機器（このPC）から開いたことにする."""

from __future__ import annotations

from fastapi.testclient import TestClient

FROM_THIS_PC = ("127.0.0.1", 50000)


def own_device_client(app, **kwargs) -> TestClient:
    """このPCの localhost から開いた TestClient. 既定の testclient/testserver では本人の機器と認めない."""
    kwargs.setdefault("client", FROM_THIS_PC)
    kwargs.setdefault("base_url", "http://localhost")
    return TestClient(app, **kwargs)
