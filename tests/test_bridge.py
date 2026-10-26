# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: N802
"""Bridge round-trip tests: in-app server (background thread) <-> BridgeBackend."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from davinci_mcp.bridge import resolve_bridge
from davinci_mcp.bridge.install import SCRIPT_NAME, install_bridge_script
from davinci_mcp.bridge.resolve_bridge import BridgeServer, compute_mac, serve, sign_request
from davinci_mcp.capabilities import proxy_class
from davinci_mcp.connection.bridge import BridgeBackend
from davinci_mcp.errors import DavinciError, ErrorCode


class FakeTimeline:
    def __str__(self) -> str:
        return "Timeline (0x3)"

    def GetName(self) -> str:
        return "TL1"


class FakeProject:
    def __init__(self) -> None:
        self.timeline = FakeTimeline()

    def __str__(self) -> str:
        return "Project (0x4)"

    def GetCurrentTimeline(self) -> FakeTimeline:
        return self.timeline

    def IsSame(self, tl: Any) -> bool:
        return tl is self.timeline

    def Nothing(self) -> None:
        return None


class FakeProjectManager:
    def __init__(self) -> None:
        self.project = FakeProject()

    def __str__(self) -> str:
        return "ProjectManager (0x2)"

    def GetCurrentProject(self) -> FakeProject:
        return self.project

    def Many(self, n: int) -> list[FakeTimeline]:
        return [FakeTimeline() for _ in range(n)]


class FakeResolve:
    def __init__(self) -> None:
        self.pm = FakeProjectManager()

    def __str__(self) -> str:
        return "Resolve (0x1)"

    def GetProductName(self) -> str:
        return "DaVinci Resolve"

    def GetProjectManager(self) -> FakeProjectManager:
        return self.pm

    def IntKeys(self) -> dict[int, str]:
        return {1: "a", 2: "b"}

    def Echo(self, value: Any) -> Any:
        return value

    def Boom(self) -> None:
        raise RuntimeError("kaput")


class Harness:
    def __init__(self, tmp_path: Path, max_handles: int = 50_000) -> None:
        self.config = tmp_path / "bridge.json"
        self.fake = FakeResolve()
        self.stop = threading.Event()
        self.server = BridgeServer(
            self.fake, self.config, idle_timeout_s=60, stop_event=self.stop, max_handles=max_handles
        )
        self.server.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def raw(self, secret: str | None = None) -> tuple[socket.socket, Any, str]:
        """Authenticated raw socket (file object, secret)."""
        secret = secret or self.server.secret
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=5)
        fh = sock.makefile("rb")
        hello = json.loads(fh.readline())
        cn = "ab" * 16
        sock.sendall(
            json.dumps(
                {
                    "type": "auth",
                    "nonce_echo": hello["nonce"],
                    "client_nonce": cn,
                    "mac": compute_mac(secret, hello["nonce"], cn),
                }
            ).encode()
            + b"\n"
        )
        return sock, fh, secret


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    h = Harness(tmp_path)
    yield h
    h.stop.set()
    h.thread.join(timeout=5)


def _signed(secret: str, **msg: Any) -> bytes:
    msg["mac"] = sign_request(secret, msg)
    return json.dumps(msg).encode() + b"\n"


def _wait(cond: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_pairing_file(harness: Harness) -> None:
    data = json.loads(harness.config.read_text())
    assert data["port"] == harness.server.port
    assert data["channel"] == "bridge-python"
    assert len(data["secret"]) == 64
    if sys.platform != "win32":  # Windows uses ACLs; chmod only toggles read-only
        assert harness.config.stat().st_mode & 0o777 == 0o600


def test_root_nested_and_types(harness: Harness) -> None:
    backend = BridgeBackend(harness.config, timeout_s=5)
    root = backend.connect()
    assert backend.channel == "bridge-python"
    assert root.GetProductName() == "DaVinci Resolve"
    pm = root.GetProjectManager()
    project = pm.GetCurrentProject()
    tl = project.GetCurrentTimeline()
    assert tl.GetName() == "TL1"
    assert project.Nothing() is None
    assert root.IntKeys() == {1: "a", 2: "b"}
    assert root.Echo({3: [1, 2.5, None, True], "x": {4: "y"}}) == {
        3: [1, 2.5, None, True],
        "x": {4: "y"},
    }
    assert project.IsSame(tl) is True
    assert tl == tl  # noqa: PLR0124
    assert len(pm.Many(3)) == 3
    backend.close()


def test_dir_str_and_proxy_class(harness: Harness) -> None:
    backend = BridgeBackend(harness.config, timeout_s=5)
    root = backend.connect()
    assert "GetProductName" in dir(root)
    assert not any(n.startswith("_") for n in dir(root))
    assert proxy_class(root) == "Resolve"
    assert proxy_class(root.GetProjectManager()) == "ProjectManager"
    assert str(root).startswith("Resolve (bridge handle ")
    backend.close()


def test_unknown_method_mirrors_real_proxy(harness: Harness) -> None:
    backend = BridgeBackend(harness.config, timeout_s=5)
    root = backend.connect()
    assert root.DoesNotExist is None
    with pytest.raises(TypeError):
        root.DoesNotExist()  # pyright: ignore[reportOptionalCall] - intentional
    backend.close()


def test_remote_exception(harness: Harness) -> None:
    backend = BridgeBackend(harness.config, timeout_s=5)
    root = backend.connect()
    with pytest.raises(DavinciError) as exc:
        root.Boom()
    assert exc.value.code == ErrorCode.API_CALL_FAILED
    assert "kaput" in exc.value.message
    backend.close()


def test_missing_pairing_file(tmp_path: Path) -> None:
    backend = BridgeBackend(tmp_path / "nope.json")
    with pytest.raises(DavinciError) as exc:
        backend.connect()
    assert exc.value.code == ErrorCode.NOT_CONNECTED
    assert exc.value.hint is not None
    assert "Workspace > Scripts > davinci_mcp_bridge" in exc.value.hint


def test_wrong_secret_rejected(harness: Harness, tmp_path: Path) -> None:
    cfg = json.loads(harness.config.read_text())
    cfg["secret"] = "00" * 32
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(cfg))
    with pytest.raises(DavinciError) as exc:
        BridgeBackend(bad, timeout_s=5).connect()
    assert exc.value.code == ErrorCode.NOT_CONNECTED
    # The server survives and still serves the legitimate client.
    assert BridgeBackend(harness.config, timeout_s=5).connect().GetProductName()


def test_replayed_seq_rejected(harness: Harness) -> None:
    sock, fh, secret = harness.raw()
    assert json.loads(fh.readline())["type"] == "ready"
    req = _signed(secret, id=1, seq=1, op="root")
    sock.sendall(req)
    assert json.loads(fh.readline())["ok"] is True
    sock.sendall(req)
    reply = json.loads(fh.readline())
    assert reply["type"] == "error"
    assert reply["error"]["type"] == "ReplayError"
    sock.close()


def test_bad_request_mac_rejected(harness: Harness) -> None:
    sock, fh, _secret = harness.raw()
    fh.readline()
    sock.sendall(json.dumps({"id": 1, "seq": 1, "op": "root", "mac": "00"}).encode() + b"\n")
    assert json.loads(fh.readline())["error"]["type"] == "AuthError"
    sock.close()


def test_oversized_preauth_line_rejected(harness: Harness) -> None:
    sock = socket.create_connection(("127.0.0.1", harness.server.port), timeout=5)
    fh = sock.makefile("rb")
    assert json.loads(fh.readline())["type"] == "hello"
    sock.sendall(b"x" * 10_000)
    try:
        data = fh.readline()
    except ConnectionError:
        data = b""
    assert data == b"" or json.loads(data)["error"]["type"] == "LineTooLong"
    sock.close()
    assert BridgeBackend(harness.config, timeout_s=5).connect().GetProductName()


def test_second_client_rejected_busy(harness: Harness) -> None:
    backend = BridgeBackend(harness.config, timeout_s=5)
    backend.connect()
    sock = socket.create_connection(("127.0.0.1", harness.server.port), timeout=5)
    msg = json.loads(sock.makefile("rb").readline())
    assert msg["error"]["type"] == "Busy"
    sock.close()
    backend.close()


def test_handle_release_on_disconnect(harness: Harness) -> None:
    backend = BridgeBackend(harness.config, timeout_s=5)
    root = backend.connect()
    root.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    assert harness.server.handle_count >= 4
    backend.close()
    assert _wait(lambda: harness.server.handle_count == 0)


def test_explicit_release_and_expired_handle(harness: Harness) -> None:
    sock, fh, secret = harness.raw()
    fh.readline()
    sock.sendall(_signed(secret, id=1, seq=1, op="root"))
    root = json.loads(fh.readline())["value"]["$handle"]
    sock.sendall(
        _signed(secret, id=2, seq=2, op="call", handle=root, method="GetProjectManager", args=[])
    )
    pm = json.loads(fh.readline())["value"]["$handle"]
    sock.sendall(_signed(secret, id=3, seq=3, op="release", handle=pm))
    assert json.loads(fh.readline())["ok"] is True
    sock.sendall(_signed(secret, id=4, seq=4, op="str", handle=pm))
    reply = json.loads(fh.readline())
    assert reply["ok"] is False
    assert reply["error"]["message"] == "handle expired"
    sock.close()


def test_lru_eviction(tmp_path: Path) -> None:
    h = Harness(tmp_path, max_handles=5)
    try:
        backend = BridgeBackend(h.config, timeout_s=5)
        root = backend.connect()
        first = root.GetProjectManager()
        for _ in range(10):
            root.GetProjectManager()
        assert root.GetProductName() == "DaVinci Resolve"  # root is pinned
        with pytest.raises(DavinciError) as exc:
            first.GetCurrentProject()
        assert exc.value.code == ErrorCode.WRONG_STATE
        assert h.server.handle_count <= 5
        backend.close()
    finally:
        h.stop.set()
        h.thread.join(timeout=5)


def test_serve_idle_shutdown(tmp_path: Path) -> None:
    cfg = tmp_path / "idle.json"
    ready = threading.Event()
    t = threading.Thread(
        target=serve,
        args=(FakeResolve(), cfg),
        kwargs={"idle_timeout_s": 0.4, "ready_event": ready},
    )
    t.start()
    assert ready.wait(5)
    assert cfg.exists()
    t.join(timeout=5)
    assert not t.is_alive()
    assert not cfg.exists()


def test_default_config_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DAVINCI_MCP_BRIDGE_CONFIG", str(tmp_path / "x.json"))
    assert resolve_bridge.default_config_path() == tmp_path / "x.json"


def test_install_bridge_script(tmp_path: Path) -> None:
    dest = install_bridge_script(tmp_path / "Utility")
    assert dest.name == SCRIPT_NAME
    assert dest.read_text() == Path(resolve_bridge.__file__).read_text()
