#!/usr/bin/env python3
"""garmin.py - every operation the Garmin MCP exposes, without the MCP.

The MCP server (Taxuspt/garmin_mcp) is a normal Python package: each module
holds `configure(client)` + `register_tools(app)`, and every tool is an async
function decorated with `@app.tool()`. Nothing in those modules needs the MCP
protocol - only `__init__.py` does. So instead of re-implementing ~12k lines,
this hands the modules a fake `app` that collects the functions into a dict,
and calls them directly. Every tool, all its curation logic, zero MCP.

    ./garmin.py list                       every tool + signature
    ./garmin.py get_sleep_data 2026-09-05  run one
    ./garmin.py raw get_sleep_data 2026-09-05   uncurated client method
    ./garmin.py api /usersummary-service/...    any Garmin endpoint
    ./garmin.py batch calls.json           many calls, ONE login
    ./garmin.py repl                       interactive
    ./garmin.py login                      first-time auth (handles MFA)
    ./garmin.py doctor                     is this thing working?

From Python:

    from garmin import G
    G.get_sleep_data("2026-09-05")     # tool (curated, same as MCP)
    G.raw.get_sleep_data("2026-09-05") # raw client method (full payload)
    G.api("/userprofile-service/socialProfile")
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import pkgutil
import sys
import threading
import time
import typing

__all__ = ["G", "connect", "reset", "tools", "call", "raw", "api", "TOKEN_DIR",
           "force_utf8_stdio"]

# Same location the MCP uses, so tokens already stored by `garmin-mcp-auth`
# are reused as-is. ~6-month OAuth tokens; the client refreshes them itself.
TOKEN_DIR = os.path.expanduser(os.getenv("GARMINTOKENS") or "~/.garminconnect")

DEFAULT_TIMEOUT = float(os.getenv("GARMIN_CALL_TIMEOUT", "90") or 0)
# Auth should answer faster than a data pull, and it runs up to
# GARMIN_LOGIN_RETRIES times - keep the worst case well inside the
# scheduled task's own 10-minute kill limit.
LOGIN_TIMEOUT = float(os.getenv("GARMIN_LOGIN_TIMEOUT", "60") or 0)

_client = None
_tools: dict | None = None


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------
def connect(interactive: bool = False):
    """Return a logged-in Garmin client, reusing the cached OAuth tokens.

    Cached after the first call, so a script that makes twenty calls logs in
    once. `interactive=True` allows a fresh email/password + MFA login.

    A login/refresh call occasionally fails transiently (a network blip, a
    slow response from Garmin's auth service) rather than because the
    tokens are actually unusable. Retrying a few times before giving up
    turns that kind of one-off hiccup into a delay instead of a full sync
    failure for the day.

    Only network-shaped failures get retried. An authentication error or a
    429 means Garmin has already rejected this token/IP - hammering that
    with fast retries doesn't recover it, it deepens the block (confirmed:
    ~15-20 rapid auth retries during same-day debugging turned one bad
    login into several hours of hard lockout). Those fail immediately.
    """
    global _client
    if _client is not None:
        return _client

    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectTooManyRequestsError,
    )

    is_cn = os.getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")

    attempts = int(os.getenv("GARMIN_LOGIN_RETRIES", "3"))
    delays = [5, 15, 30]
    last_err = None
    for attempt in range(attempts):
        try:
            client = Garmin(is_cn=is_cn)
            # Bounded like every other Garmin call. Without this the login /
            # token-refresh could hang forever: `raw()`/`api()` call connect()
            # OUTSIDE their own _run_bounded wrapper, so a stalled auth request
            # had no timeout at all. Unattended that meant the whole sync hung
            # until Task Scheduler killed it (exit 0xC000013A) - no data, no
            # push, and no recorded reason.
            _run_bounded(lambda: client.login(TOKEN_DIR), LOGIN_TIMEOUT)
            _client = client
            return _client
        except (GarminConnectAuthenticationError, GarminConnectTooManyRequestsError) as exc:
            # Not retryable: retrying just adds more failed attempts against
            # an already-rejecting endpoint.
            last_err = exc
            break
        except Exception as exc:
            last_err = exc
            if attempt < attempts - 1:
                wait = delays[min(attempt, len(delays) - 1)]
                print(f"  (login attempt {attempt + 1}/{attempts} failed: "
                      f"{type(exc).__name__}: {exc} - retrying in {wait}s)",
                      file=sys.stderr)
                time.sleep(wait)

    if not interactive:
        raise SystemExit(
            f"No usable Garmin tokens in {TOKEN_DIR} "
            f"({type(last_err).__name__}: {last_err}).\nRun:  {sys.argv[0]} login"
        )
    _client = _interactive_login(is_cn)
    return _client


def reset():
    """Drop the cached client so the next call re-authenticates from scratch.

    Useful for a caller that wants to retry a whole pull after a failure -
    without this, `_client` would stay in whatever half-broken state the
    failed attempt left it in, and a retry would just reuse that.
    """
    global _client
    _client = None


def _interactive_login(is_cn: bool):
    """Email + password (+ MFA) login, then persist tokens for next time."""
    import getpass

    from garminconnect import Garmin
    from garmin_mcp import token_utils

    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")

    client = Garmin(
        email=email, password=password, is_cn=is_cn,
        prompt_mfa=lambda: input("MFA code: ").strip(), return_on_mfa=True,
    )
    result, state = client.login()
    if result == "needs_mfa":
        client.resume_login(state, input("MFA code: ").strip())

    client.garth.dump(TOKEN_DIR) if hasattr(client, "garth") else client.client.dump(TOKEN_DIR)
    token_utils.secure_token_dir(TOKEN_DIR)  # 0700 - these are bearer creds
    print(f"Tokens stored in {TOKEN_DIR}; future runs need no password.", file=sys.stderr)
    return client


# --------------------------------------------------------------------------
# tool harvesting
# --------------------------------------------------------------------------
class _Collector:
    """Stands in for FastMCP. `@app.tool()` files the function instead of
    registering it over a protocol. Modules return the app, so it chains."""

    def __init__(self):
        self.tools: dict[str, typing.Callable] = {}

    def tool(self, *args, **kwargs):
        explicit = kwargs.get("name") or (
            args[0] if args and isinstance(args[0], str) else None
        )

        def wrapper(fn):
            self.tools[explicit or fn.__name__] = fn
            return fn

        return wrapper

    def resource(self, *args, **kwargs):  # workout_templates registers these
        return lambda fn: fn


class _FastMCPUnavailable:
    """Only reachable if upstream starts really using FastMCP outside __init__."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "garmin_mcp tried to construct a real FastMCP server, but the `mcp` "
            "package is not installed. Install it (pip install mcp) or report this "
            "-- this script is meant to never need it."
        )


def _stub_mcp_if_absent() -> None:
    """Make `mcp` an optional dependency.

    `garmin_mcp/__init__.py` does `from mcp.server.fastmcp import FastMCP` at
    module level, but nothing reachable from here ever touches it -- the tool
    modules carry no MCP dependency. Installing `mcp` just to satisfy that one
    line drags in pydantic, starlette, uvicorn, anyio and friends: ~30 MB on a
    pip-installed host, for an import we never use. So when the real package is
    absent, satisfy the import with a stub instead of requiring it.

    When `mcp` IS installed (a machine that also runs the real server) this does
    nothing and the genuine import happens, so behaviour never diverges.
    """
    import types

    if importlib.util.find_spec("mcp") is not None:
        return

    mcp = sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    server = sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    fastmcp = sys.modules.setdefault("mcp.server.fastmcp", types.ModuleType("mcp.server.fastmcp"))
    fastmcp.FastMCP = _FastMCPUnavailable
    server.fastmcp = fastmcp
    mcp.server = server


def tools(interactive: bool = False) -> dict:
    """Every tool the MCP would expose, as {name: async fn}, bound to a client.

    Modules are discovered rather than listed, so upgrading the upstream
    package picks up new tool modules with no edit here.
    """
    global _tools
    if _tools is not None:
        return _tools

    client = connect(interactive)
    _stub_mcp_if_absent()
    import garmin_mcp

    app = _Collector()
    for info in sorted(pkgutil.iter_modules(garmin_mcp.__path__), key=lambda i: i.name):
        # `__main__` boots the actual MCP server on import (it calls main()),
        # so importing it here would hand stdin to a stdio JSON-RPC loop.
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"garmin_mcp.{info.name}")
        if hasattr(module, "configure") and hasattr(module, "register_tools"):
            module.configure(client)
            module.register_tools(app)

    _tools = app.tools
    return _tools


# --------------------------------------------------------------------------
# calling
# --------------------------------------------------------------------------
def _run_bounded(fn, timeout: float):
    """Run `fn()` on a daemon thread, abandoning it after `timeout` seconds.

    Garmin occasionally stalls a single request forever; the underlying calls
    are blocking, so `asyncio.wait_for` cannot interrupt them. Mirrors the
    MCP's own guard. timeout<=0 disables the bound.
    """
    if not timeout:
        return fn()

    outcome: dict = {}

    def worker():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # replayed in the caller below
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(
            f"Garmin request did not return within {timeout:g}s (transient stall on "
            f"their side - retry, or set GARMIN_CALL_TIMEOUT=0 to wait forever)."
        )
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def call(name: str, *args, _timeout: float | None = None, **kwargs):
    """Run one MCP tool by name, synchronously."""
    registry = tools()
    fn = registry.get(name)
    if fn is None:
        raise KeyError(f"No such tool: {name!r}. Try: {sys.argv[0]} search {name}")
    timeout = DEFAULT_TIMEOUT if _timeout is None else _timeout
    return _run_bounded(lambda: asyncio.run(fn(*args, **kwargs)), timeout)


def raw(method: str, *args, _timeout: float | None = None, **kwargs):
    """Call a garminconnect client method directly - uncurated, full payload.

    The MCP tools trim their output to keep an LLM's context small. A script
    usually wants everything, so this is the escape hatch.
    """
    client = connect()
    fn = getattr(client, method, None)
    if not callable(fn):
        raise KeyError(f"No such client method: {method!r}")
    timeout = DEFAULT_TIMEOUT if _timeout is None else _timeout
    return _run_bounded(lambda: fn(*args, **kwargs), timeout)


def api(path: str, body=None, method: str | None = None, _timeout: float | None = None):
    """Hit any Garmin Connect endpoint, including ones neither side wraps."""
    client = connect()
    timeout = DEFAULT_TIMEOUT if _timeout is None else _timeout
    verb = method or ("POST" if body is not None else "GET")
    if verb.upper() == "GET":
        return _run_bounded(lambda: client.connectapi(path), timeout)
    return _run_bounded(
        lambda: client.connectapi(path, method=verb.upper(), json=body), timeout
    )


class _Facade:
    """`G.get_sleep_data(...)` - tools first, client methods as fallback."""

    raw = staticmethod(raw)
    api = staticmethod(api)
    call = staticmethod(call)
    connect = staticmethod(connect)
    tools = staticmethod(tools)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in tools():
            return lambda *a, **k: call(name, *a, **k)
        if callable(getattr(connect(), name, None)):
            return lambda *a, **k: raw(name, *a, **k)
        raise AttributeError(f"No Garmin tool or client method named {name!r}")

    def __dir__(self):
        return sorted(set(tools()) | {"raw", "api", "call", "connect", "tools"})


G = _Facade()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def force_utf8_stdio() -> None:
    """Windows consoles still default to a legacy code page (cp1252 here), and
    writing Hebrew or a `·` to one raises UnicodeEncodeError. POSIX terminals
    are already UTF-8, so this is a no-op there. Called from the CLI entry
    points; importers can call it too."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # a pipe or a stream that cannot be reconfigured

def _signature(fn) -> str:
    parts = []
    for p in inspect.signature(fn).parameters.values():
        ann = getattr(p.annotation, "__name__", str(p.annotation)).replace("typing.", "")
        if p.default is inspect.Parameter.empty:
            parts.append(f"{p.name}:{ann}")
        else:
            parts.append(f"[{p.name}={p.default!r}]")
    return " ".join(parts)


def _coerce(value: str, annotation):
    """CLI strings -> Python. Dates stay strings; numbers/bools/JSON convert."""
    if annotation is str:
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _parse_args(fn, argv: list[str]):
    """Accept positionals, --key value and --key=value (dashes -> underscores)."""
    params = inspect.signature(fn).parameters
    positional = [p for p in params.values()]
    args, kwargs, i = [], {}, 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--"):
            key, _, inline = token[2:].partition("=")
            key = key.replace("-", "_")
            if not _:
                i += 1
                inline = argv[i] if i < len(argv) else "true"
            param = params.get(key)
            kwargs[key] = _coerce(inline, param.annotation if param else str)
        else:
            param = positional[len(args)] if len(args) < len(positional) else None
            args.append(_coerce(token, param.annotation if param else str))
        i += 1
    return args, kwargs


def _emit(result, pretty: bool):
    if isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2 if pretty else None, ensure_ascii=False, default=str))
        return
    if pretty and isinstance(result, str):
        try:
            print(json.dumps(json.loads(result), indent=2, ensure_ascii=False))
            return
        except (ValueError, TypeError):
            pass
    print(result)


def _cmd_list(pattern: str | None):
    registry = tools()
    names = sorted(n for n in registry if not pattern or pattern.lower() in n.lower())
    for name in names:
        print(f"{name:42} {_signature(registry[name])}")
    sys.stdout.flush()  # keep the count below the list when stdout is a pipe
    print(f"\n{len(names)} tool(s)" + (f" matching {pattern!r}" if pattern else ""),
          file=sys.stderr)


def _cmd_help(name: str):
    fn = tools().get(name)
    if fn is None:
        return _cmd_list(name)
    print(f"{name} {_signature(fn)}\n")
    print(inspect.getdoc(fn) or "(no docstring)")


def _unwrap(value):
    """Most tools return JSON *as a string*; nesting that inside batch output
    would double-encode it and defeat jq. Parse it back when it is JSON.

    Some list-valued responses (e.g. get_training_readiness) go one level
    further and JSON-encode each item individually rather than the list as a
    whole, so a single top-level parse leaves a list of strings instead of a
    list of dicts. Recurse into list items too so callers always get real
    objects, not one more layer of string left to parse themselves.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    return value


def _cmd_batch(source: str, pretty: bool):
    """One login, N calls. `[{"tool": "...", "args": [...], "kwargs": {...}}]`"""
    text = (sys.stdin.read() if source == "-"
            else open(source, encoding="utf-8").read())
    results = []
    for item in json.loads(text):
        name = item["tool"]
        try:
            value = call(name, *item.get("args", []), **item.get("kwargs", {}))
            results.append({"tool": name, "ok": True, "result": _unwrap(value)})
        except Exception as exc:
            results.append({"tool": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    _emit(results, pretty)


def _cmd_doctor():
    print(f"token dir     : {TOKEN_DIR} ({'present' if os.path.isdir(TOKEN_DIR) else 'MISSING'})")
    import importlib.metadata as meta

    for pkg in ("garminconnect", "garmin-mcp"):
        try:
            print(f"{pkg:14}: {meta.version(pkg)}")
        except Exception:
            print(f"{pkg:14}: not installed")
    try:
        name = raw("get_full_name")
        print(f"live call     : OK - logged in as {name}")
        print(f"tools loaded  : {len(tools())}")
    except SystemExit as exc:
        print(f"live call     : FAILED\n{exc}")
        return 1
    except Exception as exc:
        print(f"live call     : FAILED - {type(exc).__name__}: {exc}")
        return 1
    return 0


def _cmd_repl():
    registry = tools()
    print(f"{len(registry)} tools loaded, one login held open. "
          f"'<tool> args', 'raw <method> args', '?<substr>', Ctrl-D to quit.")
    while True:
        try:
            line = input("garmin> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("?"):
            _cmd_list(line[1:].strip() or None)
            continue
        parts = line.split()
        try:
            if parts[0] == "raw":
                _emit(raw(parts[1], *[_coerce(a, None) for a in parts[2:]]), True)
            elif parts[0] == "api":
                _emit(api(parts[1]), True)
            elif parts[0] in registry:
                args, kwargs = _parse_args(registry[parts[0]], parts[1:])
                _emit(call(parts[0], *args, **kwargs), True)
            else:
                print(f"unknown: {parts[0]} (try ?{parts[0]})")
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}")


def main(argv: list[str]) -> int:
    force_utf8_stdio()
    if not argv or argv[0] in ("-h", "--help", "help") and len(argv) == 1:
        print(__doc__)
        return 0

    pretty = False
    for flag in ("-p", "--pretty"):
        if flag in argv:
            argv.remove(flag)
            pretty = True

    command, rest = argv[0], argv[1:]

    if command in ("list", "search", "ls"):
        _cmd_list(rest[0] if rest else None)
        return 0
    if command == "help":
        _cmd_help(rest[0])
        return 0
    if command == "login":
        connect(interactive=True)
        print("Logged in.")
        return 0
    if command == "doctor":
        return _cmd_doctor()
    if command == "repl":
        return _cmd_repl()
    if command == "batch":
        _cmd_batch(rest[0] if rest else "-", pretty)
        return 0
    if command == "raw":
        _emit(raw(rest[0], *[_coerce(a, None) for a in rest[1:]]), pretty)
        return 0
    if command == "api":
        body = json.loads(rest[1]) if len(rest) > 1 else None
        _emit(api(rest[0], body), pretty)
        return 0

    registry = tools()
    if command not in registry:
        print(f"Unknown command or tool: {command!r}\n", file=sys.stderr)
        _cmd_list(command)
        return 2
    args, kwargs = _parse_args(registry[command], rest)
    _emit(call(command, *args, **kwargs), pretty)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
