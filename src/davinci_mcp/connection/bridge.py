# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: ANN401
"""Bridge backend: talks to the in-app bridge server over an authenticated loopback socket."""

from __future__ import annotations

import json
import secrets
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

from davinci_mcp.bridge.resolve_bridge import (
    MAX_LINE,
    PROTOCOL_VERSION,
    compute_mac,
    default_config_path,
    sign_request,
)
from davinci_mcp.errors import DavinciError, ErrorCode

if TYPE_CHECKING:
    from davinci_mcp.connection.backend import Channel, ResolveObject

_START_HINT = "Start Workspace > Scripts > davinci_mcp_bridge in Resolve"


class RemoteProxy:
    """Duck-typed stand-in for a Resolve proxy object living in the bridge process."""

    __slots__ = ("_conn", "cls", "handle")

    def __init__(self, conn: _Connection, handle: str, cls: str) -> None:
        self._conn = conn
        self.handle = handle
        self.cls = cls

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        # Mirror real Resolve proxies: unknown names yield None (calling them raises TypeError).
        if name not in self._conn.methods_of(self):
            return None
        return self._conn.bound_call(self, name)

    def __dir__(self) -> list[str]:
        return list(self._conn.methods_of(self))

    def __str__(self) -> str:
        return f"{self.cls} (bridge handle {self.handle})"

    __repr__ = __str__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RemoteProxy) and other.handle == self.handle

    def __hash__(self) -> int:
        return hash(self.handle)


class _Connection:
    def __init__(self, sock: socket.socket, secret: str, timeout_s: float) -> None:
        sock.settimeout(timeout_s)
        self._sock = sock
        self._file: IO[bytes] = sock.makefile("rb")
        self._secret = secret
        self._lock = threading.Lock()
        self._seq = 0
        self._id = 0
        self._dir_cache: dict[str, tuple[str, ...]] = {}
        self.closed = False

    def close(self) -> None:
        self.closed = True
        try:
            self._file.close()
            self._sock.close()
        except OSError:
            pass

    # -- wire --------------------------------------------------------------------------------

    def _send(self, msg: dict[str, Any]) -> None:
        try:
            self._sock.sendall(json.dumps(msg, ensure_ascii=True).encode("ascii") + b"\n")
        except TimeoutError as exc:
            self.close()
            raise DavinciError(ErrorCode.TIMEOUT, "Bridge send timed out.") from exc
        except OSError as exc:
            self.close()
            raise DavinciError(
                ErrorCode.CONNECTION_LOST, f"Bridge connection lost: {exc}", hint=_START_HINT
            ) from exc

    def _recv(self) -> dict[str, Any]:
        try:
            line = self._file.readline(MAX_LINE + 2)
        except TimeoutError as exc:
            self.close()  # the stream is desynchronized after a timeout
            raise DavinciError(ErrorCode.TIMEOUT, "Bridge did not answer in time.") from exc
        except OSError as exc:
            self.close()
            raise DavinciError(
                ErrorCode.CONNECTION_LOST, f"Bridge connection lost: {exc}", hint=_START_HINT
            ) from exc
        if not line.endswith(b"\n"):
            self.close()
            raise DavinciError(
                ErrorCode.CONNECTION_LOST, "Bridge closed the connection.", hint=_START_HINT
            )
        data: Any = json.loads(line)
        if not isinstance(data, dict):
            self.close()
            raise DavinciError(ErrorCode.INTERNAL, "Malformed bridge message.")
        return cast("dict[str, Any]", data)

    def handshake(self) -> None:
        def fail(detail: str) -> DavinciError:
            self.close()
            return DavinciError(
                ErrorCode.NOT_CONNECTED,
                f"Bridge handshake failed: {detail}",
                hint=f"{_START_HINT} (the pairing file may be stale).",
            )

        hello = self._recv()
        if hello.get("type") != "hello" or hello.get("version") != PROTOCOL_VERSION:
            raise fail("unexpected greeting or protocol version")
        server_nonce = str(hello["nonce"])
        client_nonce = secrets.token_hex(16)
        self._send(
            {
                "type": "auth",
                "nonce_echo": server_nonce,
                "client_nonce": client_nonce,
                "mac": compute_mac(self._secret, server_nonce, client_nonce),
            }
        )
        try:
            ready = self._recv()
        except DavinciError as exc:
            raise fail("server rejected authentication") from exc
        expected = compute_mac(self._secret, client_nonce, server_nonce)
        if ready.get("type") != "ready" or not secrets.compare_digest(
            str(ready.get("mac", "")), expected
        ):
            raise fail(
                str(cast("dict[str, Any]", ready.get("error", {})).get("message", "bad ready"))
            )

    # -- requests ----------------------------------------------------------------------------

    def request(
        self,
        op: str,
        handle: str | None = None,
        method: str | None = None,
        args: list[Any] | None = None,
    ) -> Any:
        with self._lock:
            if self.closed:
                raise DavinciError(
                    ErrorCode.CONNECTION_LOST, "Bridge connection is closed.", hint=_START_HINT
                )
            self._seq += 1
            self._id += 1
            msg: dict[str, Any] = {"id": self._id, "seq": self._seq, "op": op}
            if handle is not None:
                msg["handle"] = handle
            if method is not None:
                msg["method"] = method
            if args is not None:
                msg["args"] = [self._encode(a) for a in args]
            msg["mac"] = sign_request(self._secret, dict(msg))
            self._send(msg)
            resp = self._recv()
        if resp.get("type") == "error" or not resp.get("ok", False):
            err = cast("dict[str, Any]", resp.get("error", {}))
            kind = str(err.get("type", "Error"))
            text = str(err.get("message", "bridge error"))
            if resp.get("type") == "error":
                self.close()
            code = ErrorCode.WRONG_STATE if kind == "HandleExpired" else ErrorCode.API_CALL_FAILED
            raise DavinciError(code, f"{kind}: {text}", details={"remote_type": kind})
        return self._decode(resp.get("value"))

    def methods_of(self, proxy: RemoteProxy) -> tuple[str, ...]:
        cached = self._dir_cache.get(proxy.cls)
        if cached is None:
            names = cast("list[str]", self.request("dir", handle=proxy.handle))
            cached = tuple(names)
            self._dir_cache[proxy.cls] = cached
        return cached

    def bound_call(self, proxy: RemoteProxy, name: str) -> Callable[..., Any]:
        def call(*args: Any) -> Any:
            return self.request("call", handle=proxy.handle, method=name, args=list(args))

        call.__name__ = name
        return call

    # -- value encoding ----------------------------------------------------------------------

    def _encode(self, value: Any) -> Any:
        if isinstance(value, RemoteProxy):
            return {"$handle": value.handle}
        if isinstance(value, (list, tuple)):
            return [self._encode(v) for v in cast("list[Any]", value)]
        if isinstance(value, dict):
            items = list(cast("dict[Any, Any]", value).items())
            if all(isinstance(k, str) and not k.startswith("$") for k, _ in items):
                return {k: self._encode(v) for k, v in items}
            return {"$dict": [[self._encode(k), self._encode(v)] for k, v in items]}
        return value

    def _decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._decode(v) for v in cast("list[Any]", value)]
        if isinstance(value, dict):
            d = cast("dict[str, Any]", value)
            if "$handle" in d:
                return RemoteProxy(self, str(d["$handle"]), str(d.get("class", "Unknown")))
            if "$dict" in d:
                return {self._decode(k): self._decode(v) for k, v in d["$dict"]}
            return {k: self._decode(v) for k, v in d.items()}
        return value


class BridgeBackend:
    channel: Channel = "bridge-python"

    def __init__(self, config_path: Path | None = None, timeout_s: float = 30.0) -> None:
        self._config_path = config_path or default_config_path()
        self._timeout_s = timeout_s
        self._conn: _Connection | None = None

    def _not_connected(self, message: str) -> DavinciError:
        return DavinciError(ErrorCode.NOT_CONNECTED, message, hint=_START_HINT)

    def _load_config(self) -> dict[str, Any]:
        try:
            raw: Any = json.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise self._not_connected(
                f"Bridge pairing file not found at {self._config_path}."
            ) from exc
        except (OSError, ValueError) as exc:
            raise self._not_connected(f"Bridge pairing file unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise self._not_connected("Bridge pairing file is malformed.")
        cfg = cast("dict[str, Any]", raw)
        port, secret = cfg.get("port"), cfg.get("secret")
        if cfg.get("version") != PROTOCOL_VERSION or not isinstance(port, int):
            raise self._not_connected("Bridge pairing file has an unsupported format.")
        if not isinstance(secret, str):
            raise self._not_connected("Bridge pairing file has no secret.")
        if cfg.get("channel") == "bridge-lua":
            self.channel = "bridge-lua"
        else:
            self.channel = "bridge-python"
        return cfg

    def connect(self) -> ResolveObject:
        self.close()
        cfg = self._load_config()
        try:
            sock = socket.create_connection(
                ("127.0.0.1", int(cfg["port"])), timeout=self._timeout_s
            )
        except OSError as exc:
            raise self._not_connected(
                f"Bridge not reachable on port {cfg['port']} (stale pairing file?): {exc}"
            ) from exc
        conn = _Connection(sock, str(cfg["secret"]), self._timeout_s)
        try:
            conn.handshake()
            root = conn.request("root")
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return root

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
