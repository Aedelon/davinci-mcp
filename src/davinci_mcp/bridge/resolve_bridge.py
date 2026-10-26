# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
# ruff: noqa: ANN401, UP031, N813
"""In-app bridge server, executed by Resolve (Workspace > Scripts) or imported by tests.

Standalone on purpose: stdlib only, no davinci_mcp import, because the file is copied as-is
into Resolve's scripts folder. Protocol: see docs/bridge-protocol.md.

Threading model: the whole server (accept, read, API calls) runs in the thread that calls
``serve()`` -- inside Resolve that is the script thread, so every API call executes on it.
No helper threads exist. A single ``select`` loop multiplexes the listener (to reject extra
clients) and the active client.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import select
import socket
import threading
import time
from pathlib import Path
from typing import Any, cast

PROTOCOL_VERSION = 1
CHANNEL = "bridge-python"
MAX_LINE = 4 * 1024 * 1024
MAX_PREAUTH_LINE = 4096
DEFAULT_MAX_HANDLES = 50_000
DEFAULT_IDLE_TIMEOUT_S = 30 * 60.0
AUTH_DEADLINE_S = 10.0
MIN_NONCE_LEN = 16
MAX_NONCE_LEN = 128
POLL_S = 0.25

log = logging.getLogger("davinci_mcp.bridge")


def default_config_path() -> Path:
    override = os.environ.get("DAVINCI_MCP_BRIDGE_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "davinci-mcp" / "bridge.json"


def compute_mac(secret: str, *parts: str) -> str:
    """HMAC-SHA256 keyed with the ASCII bytes of the hex secret, over the concatenated parts."""
    return hmac.new(
        secret.encode("ascii"), "".join(parts).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def canonical_json(msg: dict[str, Any]) -> str:
    """Sorted keys, no whitespace, ASCII-only escapes: the exact bytes covered by a request MAC."""
    return json.dumps(
        msg, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def sign_request(secret: str, msg: dict[str, Any]) -> str:
    """MAC of a request; ``msg`` must not contain the ``mac`` field."""
    return compute_mac(secret, canonical_json(msg))


class _ProtocolError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _RemoteError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _LineReader:
    """Accumulates bytes and yields newline-terminated lines with a hard length cap."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def pop_line(self, limit: int) -> bytes | None:
        idx = self._buf.find(b"\n")
        if idx < 0:
            if len(self._buf) > limit:
                raise _ProtocolError("LineTooLong", "line exceeds %d bytes" % limit)
            return None
        if idx > limit:
            raise _ProtocolError("LineTooLong", "line exceeds %d bytes" % limit)
        line = bytes(self._buf[:idx])
        del self._buf[: idx + 1]
        return line

    def fill(self, conn: socket.socket) -> None:
        chunk = conn.recv(65536)
        if not chunk:
            raise EOFError
        self._buf += chunk


def _class_of(obj: Any) -> str:
    text = str(obj)
    return text.split(" ", 1)[0] if text else "Unknown"


class BridgeServer:
    def __init__(
        self,
        resolve_obj: Any,
        config_path: Path,
        *,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        port: int = 0,
        stop_event: threading.Event | None = None,
        max_handles: int = DEFAULT_MAX_HANDLES,
    ) -> None:
        self._root = resolve_obj
        self._config_path = Path(config_path)
        self._idle_timeout_s = idle_timeout_s
        self._requested_port = port
        self._stop = stop_event or threading.Event()
        self._max_handles = max(2, max_handles)
        self._secret = secrets.token_hex(32)
        self._listener: socket.socket | None = None
        self._handles: collections.OrderedDict[str, Any] = collections.OrderedDict()
        self._root_handle: str | None = None
        self._next_handle = 1
        self._last_activity = time.monotonic()
        self.port = 0

    @property
    def handle_count(self) -> int:
        return len(self._handles)

    @property
    def secret(self) -> str:
        return self._secret

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self._requested_port))
        listener.listen(4)
        self._listener = listener
        self.port = int(listener.getsockname()[1])
        self._write_config()
        self._last_activity = time.monotonic()
        log.info("davinci-mcp bridge listening on 127.0.0.1:%d", self.port)

    def serve_forever(self, ready_event: threading.Event | None = None) -> None:
        if self._listener is None:
            self.start()
        listener = self._listener
        assert listener is not None
        if ready_event is not None:
            ready_event.set()
        try:
            while not self._should_stop():
                readable, _, _ = select.select([listener], [], [], POLL_S)
                if not readable:
                    continue
                conn, _addr = listener.accept()
                self._last_activity = time.monotonic()
                try:
                    self._serve_client(conn)
                except OSError as exc:
                    log.info("client connection error: %s", exc)
                finally:
                    self._handles.clear()
                    self._root_handle = None
                    conn.close()
        finally:
            listener.close()
            self._listener = None
            self._remove_config()
            log.info("davinci-mcp bridge stopped")

    def _should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        return time.monotonic() - self._last_activity > self._idle_timeout_s

    # -- pairing file ------------------------------------------------------------------------

    def _write_config(self) -> None:
        path = self._config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": PROTOCOL_VERSION,
            "port": self.port,
            "secret": self._secret,
            "pid": os.getpid(),
            "channel": CHANNEL,
        }
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload))
        os.replace(tmp, path)

    def _remove_config(self) -> None:
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid() and data.get("secret") == self._secret:
                self._config_path.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    # -- connection handling -----------------------------------------------------------------

    def _send(self, conn: socket.socket, msg: dict[str, Any]) -> None:
        conn.sendall(json.dumps(msg, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n")

    def _send_error(self, conn: socket.socket, kind: str, message: str) -> None:
        with contextlib.suppress(OSError):
            self._send(conn, {"type": "error", "error": {"type": kind, "message": message}})

    def _poll_line(self, conn: socket.socket, reader: _LineReader, limit: int) -> bytes | None:
        line = reader.pop_line(limit)
        if line is not None:
            return line
        listener = self._listener
        watch = [conn] if listener is None else [conn, listener]
        readable, _, _ = select.select(watch, [], [], POLL_S)
        if listener is not None and listener in readable:
            extra, _addr = listener.accept()
            self._send_error(extra, "Busy", "another client is already connected")
            extra.close()
        if conn in readable:
            reader.fill(conn)
        return reader.pop_line(limit)

    def _serve_client(self, conn: socket.socket) -> None:
        conn.settimeout(AUTH_DEADLINE_S)
        reader = _LineReader()
        try:
            if not self._handshake(conn, reader):
                return
            last_seq = 0
            while not self._should_stop():
                line = self._poll_line(conn, reader, MAX_LINE)
                if line is None:
                    continue
                self._last_activity = time.monotonic()
                try:
                    msg = json.loads(line)
                    last_seq = self._check_request(msg, last_seq)
                except _ProtocolError as exc:
                    self._send_error(conn, exc.kind, str(exc))
                    return
                except ValueError:
                    self._send_error(conn, "BadRequest", "invalid JSON")
                    return
                self._send(conn, self._handle_request(msg))
                self._last_activity = time.monotonic()
        except EOFError:
            pass
        except ConnectionError:
            pass
        except _ProtocolError as exc:
            self._send_error(conn, exc.kind, str(exc))

    def _handshake(self, conn: socket.socket, reader: _LineReader) -> bool:
        nonce = secrets.token_hex(16)
        self._send(conn, {"type": "hello", "nonce": nonce, "version": PROTOCOL_VERSION})
        deadline = time.monotonic() + AUTH_DEADLINE_S
        line: bytes | None = None
        while line is None:
            if time.monotonic() > deadline or self._stop.is_set():
                self._send_error(conn, "AuthTimeout", "authentication timed out")
                return False
            line = self._poll_line(conn, reader, MAX_PREAUTH_LINE)
        try:
            msg = json.loads(line)
            client_nonce = msg["client_nonce"]
            if (
                msg.get("type") != "auth"
                or msg.get("nonce_echo") != nonce
                or not isinstance(client_nonce, str)
                or not MIN_NONCE_LEN <= len(client_nonce) <= MAX_NONCE_LEN
            ):
                raise ValueError  # noqa: TRY301
            int(client_nonce, 16)
            expected = compute_mac(self._secret, nonce, client_nonce)
            if not hmac.compare_digest(str(msg.get("mac", "")), expected):
                raise ValueError  # noqa: TRY301
        except (ValueError, KeyError, TypeError, AttributeError):
            self._send_error(conn, "AuthError", "authentication failed")
            return False
        self._send(conn, {"type": "ready", "mac": compute_mac(self._secret, client_nonce, nonce)})
        return True

    def _check_request(self, msg: Any, last_seq: int) -> int:
        if not isinstance(msg, dict):
            raise _ProtocolError("BadRequest", "request must be an object")
        body: dict[str, Any] = dict(msg)  # pyright: ignore[reportUnknownArgumentType]
        mac = body.pop("mac", None)
        seq = body.get("seq")
        if not isinstance(mac, str) or not isinstance(seq, int) or isinstance(seq, bool):
            raise _ProtocolError("AuthError", "missing mac or seq")
        try:
            expected = sign_request(self._secret, body)
        except (TypeError, ValueError) as exc:
            raise _ProtocolError("BadRequest", "unencodable request") from exc
        if not hmac.compare_digest(mac, expected):
            raise _ProtocolError("AuthError", "bad request mac")
        if seq <= last_seq:
            raise _ProtocolError("ReplayError", "seq must increase monotonically")
        return seq

    # -- request execution -------------------------------------------------------------------

    def _handle_request(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = msg.get("id")
        try:
            value = self._dispatch(msg)
            response: dict[str, Any] = {"id": rid, "ok": True, "value": value}
            if len(json.dumps(response)) > MAX_LINE:
                raise _RemoteError("ResponseTooLarge", "response exceeds 4 MiB")  # noqa: TRY301
        except _RemoteError as exc:
            return {"id": rid, "ok": False, "error": {"type": exc.kind, "message": str(exc)}}
        except Exception as exc:
            return {
                "id": rid,
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        return response

    def _dispatch(self, msg: dict[str, Any]) -> Any:
        op = msg.get("op")
        if op == "root":
            return self._root_ref()
        handle = msg.get("handle")
        if op == "release":
            if isinstance(handle, str) and handle != self._root_handle:
                self._handles.pop(handle, None)
            return None
        obj = self._lookup(handle)
        if op == "dir":
            return sorted(n for n in dir(obj) if not n.startswith("_"))
        if op == "str":
            return str(obj)
        if op == "call":
            method = msg.get("method")
            if not isinstance(method, str) or method.startswith("_"):
                raise _RemoteError("BadRequest", "invalid method name")
            fn = getattr(obj, method, None)
            if not callable(fn):
                raise _RemoteError("NoSuchMethod", "%s is not callable" % method)
            raw_args = msg.get("args", [])
            if not isinstance(raw_args, list):
                raise _RemoteError("BadRequest", "args must be a list")
            args = [self._decode(a) for a in raw_args]  # pyright: ignore[reportUnknownVariableType]
            return self._encode(fn(*args))
        raise _RemoteError("BadRequest", "unknown op %r" % (op,))

    def _lookup(self, handle: Any) -> Any:
        if not isinstance(handle, str) or handle not in self._handles:
            raise _RemoteError("HandleExpired", "handle expired")
        self._handles.move_to_end(handle)
        return self._handles[handle]

    def _register(self, obj: Any) -> str:
        handle = "h%d" % self._next_handle
        self._next_handle += 1
        self._handles[handle] = obj
        while len(self._handles) > self._max_handles:
            oldest = next(iter(self._handles))
            if oldest == self._root_handle:
                self._handles.move_to_end(oldest)
                continue
            del self._handles[oldest]
        return handle

    def _ref(self, handle: str, obj: Any) -> dict[str, str]:
        return {"$handle": handle, "class": _class_of(obj)}

    def _root_ref(self) -> Any:
        if self._root is None:
            return None
        if self._root_handle is None or self._root_handle not in self._handles:
            self._root_handle = self._register(self._root)
        return self._ref(self._root_handle, self._root)

    def _encode(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, (list, tuple)):
            return [self._encode(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, dict):
            items: list[tuple[Any, Any]] = list(value.items())  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
            if all(isinstance(k, str) and not k.startswith("$") for k, _ in items):
                return {k: self._encode(v) for k, v in items}
            return {"$dict": [[self._encode(k), self._encode(v)] for k, v in items]}
        return self._ref(self._register(value), value)

    def _decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._decode(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, dict):
            value = cast("dict[str, Any]", value)
            if "$handle" in value:
                return self._lookup(value["$handle"])
            if "$dict" in value:
                pairs = cast("list[list[Any]]", value["$dict"])
                return {self._decode(k): self._decode(v) for k, v in pairs}
            return {k: self._decode(v) for k, v in value.items()}
        return value


def serve(
    resolve_obj: Any,
    config_path: Path,
    *,
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
    port: int = 0,
    ready_event: threading.Event | None = None,
    stop_event: threading.Event | None = None,
    max_handles: int = DEFAULT_MAX_HANDLES,
) -> None:
    """Run the bridge until idle timeout or ``stop_event``. Blocks the calling thread."""
    server = BridgeServer(
        resolve_obj,
        config_path,
        idle_timeout_s=idle_timeout_s,
        port=port,
        stop_event=stop_event,
        max_handles=max_handles,
    )
    server.start()
    server.serve_forever(ready_event)


def _get_resolve() -> Any:
    try:
        return resolve  # type: ignore[name-defined]  # pyright: ignore
    except NameError:
        import DaVinciResolveScript as dvr  # type: ignore[import-not-found]  # noqa: PLC0415

        return dvr.scriptapp("Resolve")  # pyright: ignore


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    minutes = float(os.environ.get("DAVINCI_MCP_BRIDGE_IDLE_MIN", "30"))
    serve(_get_resolve(), default_config_path(), idle_timeout_s=minutes * 60.0)


if __name__ == "__main__" or "resolve" in globals():
    main()
