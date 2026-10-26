# Bridge protocol (version 1)

The bridge is a small server running **inside** Resolve (launched from Workspace > Scripts) that
re-exports the scripting API over loopback. `BridgeBackend` (MCP side) is the client. Reference
implementations: `src/davinci_mcp/bridge/resolve_bridge.py` (server) and
`src/davinci_mcp/connection/bridge.py` (client). The format is deliberately simple so a LuaJIT
server can reimplement it.

## Transport

- TCP on `127.0.0.1`, one connection at a time (extra clients get `Busy` and are closed).
- Line-delimited UTF-8 JSON: one message per line (`\n`), no embedded raw newlines (JSON escapes them).
- Max line: 4 MiB (4 096 bytes before authentication). Exceeding it closes the connection.
- Messages sent by the Python reference use ASCII-only JSON (`\uXXXX` escapes).

## Pairing file

`~/.config/davinci-mcp/bridge.json` (override: env `DAVINCI_MCP_BRIDGE_CONFIG`), mode `0600`,
written atomically by the server after binding, removed on shutdown:

```json
{"version":1,"port":51234,"secret":"<64 hex chars>","pid":1234,"channel":"bridge-python"}
```

`channel` is `bridge-python` or `bridge-lua`. The secret is 32 random bytes in hex. The **key** for
every HMAC below is the 64-character hex string itself, taken as ASCII bytes (not decoded).

## Handshake (mutual authentication)

1. Server -> `{"type":"hello","nonce":"<hex>","version":1}` (nonce: 16 random bytes in hex).
2. Client -> `{"type":"auth","nonce_echo":"<server nonce>","client_nonce":"<hex, 16..128 chars>","mac":M1}`
   with `M1 = HMAC-SHA256(secret, server_nonce + client_nonce)` (string concatenation), lowercase hex.
3. Server -> `{"type":"ready","mac":M2}` with `M2 = HMAC-SHA256(secret, client_nonce + server_nonce)`.
   The client MUST verify M2 (proves the server knows the secret).

On any failure the server sends `{"type":"error","error":{"type","message"}}` and closes. Auth must
complete within 10 s.

## Requests and responses

Request (after `ready`):

```json
{"id":1,"seq":1,"op":"call","handle":"h2","method":"GetName","args":[],"mac":"<hex>"}
```

- `id`: integer chosen by the client, echoed in the response.
- `seq`: integer, strictly increasing per connection (starts at 1). A non-increasing `seq` (replay)
  yields `{"type":"error","error":{"type":"ReplayError",...}}` and closes the connection.
- `mac`: `HMAC-SHA256(secret, canonical(request without "mac"))`, lowercase hex. Invalid -> `AuthError`
  and close.
- `canonical`: JSON with keys sorted, separators `,` and `:` (no whitespace), ASCII-only escapes
  (`\uXXXX`, lowercase hex, surrogate pairs for non-BMP). Absent optional fields are omitted
  (never `null`).

Ops:

| op        | fields               | value                                                        |
|-----------|----------------------|--------------------------------------------------------------|
| `root`    | -                    | handle of the `resolve` object (pinned, never evicted)       |
| `call`    | `handle`,`method`,`args` | encoded return value. `method` starting with `_` is refused |
| `dir`     | `handle`             | sorted list of attribute names not starting with `_`         |
| `str`     | `handle`             | `str(obj)`                                                   |
| `release` | `handle`             | `null`; the handle is dropped                                |

Response: `{"id":1,"ok":true,"value":...}` or
`{"id":1,"ok":false,"error":{"type":"HandleExpired","message":"handle expired"}}`.
Error types used: `HandleExpired`, `NoSuchMethod`, `BadRequest`, `ResponseTooLarge`, or the name of the
exception raised by Resolve. Responses are not MACed (loopback, request authentication is what matters).

## Value encoding

- JSON primitives, lists (tuples become lists) and dicts with string keys map directly.
  NaN/Infinity are sent as `null`.
- Dict with non-string keys (Resolve uses int keys) or any key starting with `$`:
  `{"$dict":[[k,v],...]}`. Decoders must recognise `$dict` in both directions.
- Any other object: `{"$handle":"h17","class":"Timeline"}` where `class = str(obj).split(" ",1)[0]`.
  As an argument only `{"$handle":"h17"}` is needed.
- An empty list and an empty dict are both `[]`/`{}` on the wire; Lua implementations must emit `[]` for
  empty arrays that came from Resolve lists.

## Server semantics

- **Threading**: the server loop runs in the script thread itself (the thread that called `serve()`);
  no other thread exists. Accept, read and API calls are multiplexed with a single `select` loop, so
  every Resolve API call executes on Resolve's script thread. Long API calls block the loop.
- Handle table per connection: bounded (default 50 000), LRU eviction (a use refreshes recency, the root
  handle is pinned); an evicted or released handle yields `HandleExpired`. All handles are dropped on
  disconnect. Handle ids are fresh per call (`h<counter>`), the same Resolve object may get several ids.
- Single client at a time. Idle shutdown after `DAVINCI_MCP_BRIDGE_IDLE_MIN` minutes (default 30)
  without any request; the server then removes the pairing file.
- Unknown attribute names: the client answers from the cached `dir` list and returns `None` for names
  not listed, exactly like real Resolve proxies (calling the result raises `TypeError`).

## Lua port notes / open questions

- Needs SHA-256 + HMAC in pure Lua (no `require`/`io`/`os` in the 21.1 sandbox) and a CSPRNG source;
  `math.random` is not cryptographically secure.
- Socket availability in the sandboxed LuaJIT is unknown; if absent, a file-based or other channel is
  needed and this TCP protocol does not apply.
- Writing the pairing file needs filesystem access the sandbox may not offer.
- Canonical JSON (sorted keys, float formatting) is the hardest part to reproduce; integers and strings
  are safe. A simpler MAC input (fixed field order string) could be adopted in version 2.
- No `select`: a Lua server would need a blocking accept/read loop, so idle shutdown and the `Busy`
  rejection may not be implementable.
