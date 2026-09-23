#!/usr/bin/env python3
"""Self-check: run `.venv/bin/python test_garmin.py`.

Offline checks cover the argument parsing and tool harvesting (the parts with
branches worth breaking). The live check is last and needs valid tokens.
"""

import sys

import garmin


def test_arg_parsing():
    def sample(date: str, limit: int = 5, verbose: bool = False, tags: list = None):
        pass

    args, kwargs = garmin._parse_args(sample, ["2026-09-04", "10"])
    assert args == ["2026-09-04", 10], args  # date stays str, limit becomes int

    args, kwargs = garmin._parse_args(sample, ["--date=2026-09-04", "--limit", "3"])
    assert kwargs == {"date": "2026-09-04", "limit": 3}, kwargs

    # dashes in flags map to underscores in parameter names
    def dated(start_date: str):
        pass

    _, kwargs = garmin._parse_args(dated, ["--start-date", "2026-09-04"])
    assert kwargs == {"start_date": "2026-09-04"}, kwargs

    # a bare --flag with no value is a boolean true
    _, kwargs = garmin._parse_args(sample, ["--verbose"])
    assert kwargs == {"verbose": True}, kwargs

    # JSON-shaped values decode; a str-annotated one never does
    assert garmin._coerce("[1,2]", list) == [1, 2]
    assert garmin._coerce("2026-09-04", str) == "2026-09-04"
    assert garmin._coerce("42", str) == "42"


def test_unwrap():
    assert garmin._unwrap('{"a": 1}') == {"a": 1}
    assert garmin._unwrap("not json") == "not json"
    assert garmin._unwrap({"a": 1}) == {"a": 1}


def test_collector_matches_mcp_registration():
    """The fake app must collect what FastMCP would have registered."""
    app = garmin._Collector()

    @app.tool()
    async def some_tool(x: str) -> str:
        return x

    @app.tool(name="renamed")
    async def other(x: str) -> str:
        return x

    assert set(app.tools) == {"some_tool", "renamed"}, app.tools


def test_tools_harvested():
    """Needs tokens: proves every module registered against the real client."""
    registry = garmin.tools()
    assert len(registry) > 100, f"only {len(registry)} tools harvested"
    for expected in ("get_sleep_data", "get_activities", "create_run_workout",
                     "upsert_and_log", "schedule_week", "get_body_battery"):
        assert expected in registry, f"missing tool: {expected}"
    # __main__ must never be imported - it boots the MCP server on import
    import sys
    assert "garmin_mcp.__main__" not in sys.modules


def test_mcp_stub_satisfies_the_only_import_that_needs_it():
    """`mcp` must be optional: the stub has to satisfy
    `from mcp.server.fastmcp import FastMCP` on a host without the package."""
    import importlib.util
    import types

    real = {k: sys.modules[k] for k in list(sys.modules)
            if k == "mcp" or k.startswith("mcp.")}
    original_find_spec = importlib.util.find_spec
    try:
        for k in real:
            del sys.modules[k]
        importlib.util.find_spec = lambda name, *a, **k: (
            None if name == "mcp" else original_find_spec(name, *a, **k))

        garmin._stub_mcp_if_absent()
        exec("from mcp.server.fastmcp import FastMCP", {})   # must not raise

        from mcp.server.fastmcp import FastMCP
        assert FastMCP is garmin._FastMCPUnavailable
        try:
            FastMCP("x")
        except RuntimeError as exc:
            assert "not installed" in str(exc)
        else:
            raise AssertionError("the stub must refuse to act as a real server")
    finally:
        importlib.util.find_spec = original_find_spec
        for k in [k for k in list(sys.modules) if k == "mcp" or k.startswith("mcp.")]:
            del sys.modules[k]
        sys.modules.update(real)


def test_force_utf8_stdio_is_safe_to_call():
    """Windows consoles default to a legacy code page; this must never raise,
    including on a stream that cannot be reconfigured."""
    garmin.force_utf8_stdio()

    class Stubborn:
        encoding = "cp1252"

        def reconfigure(self, **kwargs):
            raise OSError("not reconfigurable")

    real_out, real_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = Stubborn()
        garmin.force_utf8_stdio()
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def test_live_call():
    name = garmin.raw("get_full_name")
    assert name, "logged in but got an empty name back"
    return name



def test_digest_renders_without_crashing():
    """A rest day, a missing night and a null metric must all render, not raise."""
    import daily_digest as dd

    empty = {"activities": [], "summary": {}, "sleep": {}, "readiness": [], "status": {}}
    msg, substantive = dd.render("2026-09-03", empty)
    assert "אין אימון רשום היום" in msg
    assert "אין נתוני שינה" in msg
    assert substantive is False, "an empty day must not trigger a send"

    full = {
        "activities": [{"activityType": {"typeKey": "running"}, "distance": 10000.0,
                        "duration": 3000.0, "averageHR": 150.0, "activityTrainingLoad": 90.0}],
        "summary": {"totalSteps": 12000, "totalKilocalories": 2500.0,
                    "moderateIntensityMinutes": 10, "vigorousIntensityMinutes": 20},
        "sleep": {"sleep_hours": 6.5, "sleep_score": 70, "sleep_score_qualifier": "FAIR"},
        "readiness": [{"context": "AFTER_WAKEUP_RESET", "score": 40, "level": "POOR",
                       "sleep_factor_percent": 31, "hrv_factor_percent": 90}],
        "status": {"acute_load": 451, "chronic_load": 469, "load_ratio": 0.9,
                   "acwr_status": "OPTIMAL", "vo2_max_precise": 57.8},
    }
    msg, substantive = dd.render("2026-09-04", full)
    assert substantive is True
    assert "5:00 לק\"מ" in msg, msg          # 10km in 50min
    assert "דקות עצימות 50" in msg           # moderate + 2x vigorous
    assert "ירודה" in msg                    # POOR is below LOW on Garmin's ladder
    assert "שינה 31%" in msg                 # names the factor dragging readiness down


if __name__ == "__main__":
    # A failing assertion below carries Hebrew in its message; on a legacy
    # Windows console printing that traceback would itself raise and hide
    # the real failure.
    garmin.force_utf8_stdio()

    offline = [test_arg_parsing, test_unwrap, test_collector_matches_mcp_registration,
               test_digest_renders_without_crashing,
               test_mcp_stub_satisfies_the_only_import_that_needs_it,
               test_force_utf8_stdio_is_safe_to_call]
    for check in offline:
        check()
        print(f"ok  {check.__name__}")

    test_tools_harvested()
    print(f"ok  test_tools_harvested ({len(garmin.tools())} tools)")
    print(f"ok  test_live_call (account: {test_live_call()})")
    print("\nall checks passed")
