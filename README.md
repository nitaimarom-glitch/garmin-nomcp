# garmin-nomcp

**All 148 tools of the [Garmin MCP server](https://github.com/Taxuspt/garmin_mcp) — as a CLI and a Python module. No MCP server, no LLM, no tokens.**

```
$ ./garmin doctor
token dir     : ~/.garminconnect (present)
garminconnect : 0.3.2
live call     : OK - logged in as <you>
tools loaded  : 148
```

0.8 seconds, live API call included.

## Why

The Garmin MCP is excellent, but it is only reachable from inside an LLM session. A cron job, a morning-report script, a Grafana feed, a one-off question in a terminal — none of them can call it. This makes every one of its tools an ordinary function.

It is also faster *for* an agent: no 148 tool schemas loaded into context, no round-trip per call.

## The trick

`garmin_mcp` is a normal Python package. Every module exposes `configure(client)` + `register_tools(app)`, and every tool is an `async def` decorated with `@app.tool()`. **Only `__init__.py` imports `mcp` — the tool modules have no MCP dependency at all.**

So this hands those modules a fake `app` whose `.tool()` files the function into a dict instead of registering it over a protocol, then calls the functions directly:

```python
class _Collector:
    def __init__(self): self.tools = {}
    def tool(self, *a, **k):
        def wrapper(fn):
            self.tools[fn.__name__] = fn
            return fn
        return wrapper
```

~350 lines instead of re-implementing 12,455. Every tool, with all of its custom logic intact — the workout-JSON builders, the GraphQL schedule de-duplication, the nutrition find-or-create-then-log flow, the FIT gzip/zip sniffing.

Modules are discovered with `pkgutil`, so upgrading upstream picks up new tools with no edit here.

> **Landmine, if you build something like this yourself:** `pkgutil.iter_modules` also finds `__main__`, and importing `garmin_mcp.__main__` *boots the entire MCP server* — it calls `main()` at import time. Your script then prints nothing, swallows stdin, and exits 1. Underscore-prefixed modules are skipped for exactly this reason.

The approach is not Garmin-specific. Most FastMCP servers are shaped this way.

## Install

**macOS / Linux**

```sh
git clone https://github.com/NoamVardi18/garmin-nomcp
cd garmin-nomcp
./setup.sh                # venv + deps + self-check
./setup.sh --slim         # ~30 MB smaller, see below
```

Uses [uv](https://docs.astral.sh/uv/) when it is installed and falls back to stdlib `venv` + `pip` when it is not.

**Windows**

```powershell
git clone https://github.com/NoamVardi18/garmin-nomcp
cd garmin-nomcp
.\setup.ps1               # or: .\setup.ps1 -Slim
.\garmin.cmd doctor
```

If PowerShell answers *"running scripts is disabled on this system"*, that is Windows' default execution policy, not a fault in the script. Either allow local scripts once (`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`) or bypass it for this one file:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Needs `git` on PATH — pip resolves the dependency from GitHub, and Windows does not ship git. `setup.ps1` checks for it and says so up front.

Or skip the script entirely — these three lines are all it does, and they cannot trip an execution policy:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install "garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp" "garminconnect==0.3.2"
.venv\Scripts\python test_garmin.py
```

`garmin.cmd` is the launcher — use it wherever this README says `./garmin`. Add the repo folder to your PATH and `garmin doctor` works from anywhere.

Both need Python 3.12+.

### Slim install

`garmin_mcp/__init__.py` imports `FastMCP` at module level, and nothing this script calls ever touches it. When the `mcp` package is missing, `garmin.py` satisfies that one import with a stub rather than requiring it — which drops `mcp`, `pydantic`, `starlette`, `uvicorn` and the rest of the server-side tree. On a pip-installed Linux host that is **97 MB → 63 MB**, with all 148 tools still working.

`--slim` / `-Slim` installs everything and then removes that tree. If `mcp` *is* installed, the real import happens as before, so behaviour never differs between the two.

## Auth

Reuses the OAuth tokens in `~/.garminconnect` (override with `GARMINTOKENS`). These are ~6-month bearer tokens that the client refreshes itself, so **no password and no MFA on a normal run**. If you already ran `garmin-mcp-auth`, there is nothing to do.

First time, or when they expire:

```sh
./garmin login      # email + password + MFA, once
```

Credentials are never stored — only the tokens.

On macOS and Linux the token directory is `chmod 700`. **On Windows it is not actually restricted:** Python's `os.chmod` only toggles the read-only bit there and does not touch NTFS ACLs, so the tokens inherit whatever your user profile grants. They are ~6-month bearer credentials to the whole Garmin account — on a shared Windows machine, lock the folder down yourself (`icacls`) or point `GARMINTOKENS` somewhere you control.

## Use

```sh
./garmin list [substring]                 # 148 tools + signatures
./garmin help get_sleep_summary
./garmin get_sleep_summary 2026-09-03     # curated output, same as the MCP
./garmin raw get_rhr_day 2026-09-03 -p    # uncurated client method, full payload
./garmin api /userprofile-service/socialProfile -p    # any endpoint, wrapped or not
./garmin batch calls.json -p              # N calls, ONE login, jq-able array
./garmin repl                             # interactive, login held open
```

Arguments take positionals or flags — `--start-date 2026-09-01`, `--limit=5`. Dates stay strings; everything else JSON-decodes. `-p` pretty-prints.

`ln -s "$PWD/garmin" /usr/local/bin/garmin` to call it from anywhere, cron included.

### From Python

```python
from garmin import G

G.get_sleep_summary("2026-09-03")    # tool (curated, same as the MCP)
G.raw("get_rhr_day", "2026-09-03")   # raw client method (full payload)
G.api("/userprofile-service/socialProfile")
```

One login is cached across every call in the process.

### Four surfaces, not one

| | what it gives you |
|---|---|
| `<tool>` | exactly what the MCP returns — trimmed for an LLM's context |
| `raw <method>` | the underlying `garminconnect` method, uncurated |
| `api <path>` | any Garmin Connect endpoint, including ones neither side wraps |
| `batch` | many calls, one login, one JSON array out |

`raw` matters more than it sounds. The MCP tools deliberately drop fields to protect context — `get_activities_by_date` returns `average_hr: null`. The raw call has heart rate, pace, cadence, ground-contact time, training effect and VO₂max.

## Daily digest

`daily_digest.py` renders one day as a plain-text Hebrew Telegram message — activities with pace, HR and load; steps and intensity minutes; last night's sleep; morning readiness with the factor dragging it down; acute/chronic load.

```sh
./daily_digest.py                    # today, printed
./daily_digest.py --date 2026-09-04
./daily_digest.py --send             # post it to Telegram
```

Every metric is optional. A rest day, a missing night, or a watch left on the charger shortens the message instead of raising, and a day with nothing recorded exits without sending — so cron stays quiet rather than pinging you with an empty template. Pass `--always` to send regardless.

Telegram goes through the host's own `~/.claude/hooks/tg-notify.js` when that exists, otherwise a direct API call using `GARMIN_DIGEST_BOT_TOKEN` + `GARMIN_DIGEST_CHAT_ID` (env, or `~/.garmin-digest.env`).

### As a cron job

```cron
0 20 * * * export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"; cd ~/garmin-nomcp && ./.venv/bin/python daily_digest.py --send >> ~/logs/garmin-digest.log 2>&1
```

Cron runs with a bare `PATH`, which is where jobs like this usually die silently. Rehearse it the way cron will actually run it before trusting the schedule:

```sh
env -i HOME=$HOME /bin/sh -c 'export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"; cd ~/garmin-nomcp && ./.venv/bin/python daily_digest.py'
```

## Verify

```sh
.venv/bin/python test_garmin.py
```

Argument parsing, collector parity, the `__main__` guard, 148 tools harvested, one live call.

## Pins

`garminconnect==0.3.2` on purpose — the version the MCP itself resolves to. `schedule_week` and `set_heart_rate_zones` reach past the library into `client.post` / `client.request`, a surface 0.3.x reshuffled. Newer is not better when it is untested against these 148 tools.

## Status

Read paths are verified live. Write tools (`create_*_workout`, `schedule_week`, `upsert_and_log`, `add_weigh_in`, `upload_course`) are wired and their signatures resolve, but they mutate a real Garmin account and have not been fired here. Try them on your own account before trusting them in a script.

## Credit

All 148 tools and their logic are the work of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp), built on [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect). This project adds no Garmin knowledge of its own — it is an adapter that makes theirs callable without MCP.

MIT.
