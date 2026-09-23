#!/usr/bin/env python3
"""Daily Garmin digest -> Telegram. Built for cron.

    ./daily_digest.py                 print today's digest
    ./daily_digest.py --date 2026-09-04
    ./daily_digest.py --send          post it to Telegram

Every metric is optional: a missing night, a rest day, a watch left on the
charger all produce a shorter message, never a crash. Cron gets exit 0 for
"nothing to report" and non-zero only for a real failure, so a broken token
is loud and a rest day is quiet.

Telegram config, first match wins (env vars, then ~/.garmin-digest.env,
then ~/.claude/remote-bot.env):
    GARMIN_DIGEST_BOT_TOKEN / GARMIN_DIGEST_CHAT_ID
    REMOTE_BOT_TOKEN        / REMOTE_CHAT_ID
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import garmin  # noqa: E402

HEB_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
HEB_MONTHS = ["בינואר", "בפברואר", "במרץ", "באפריל", "במאי", "ביוני", "ביולי",
              "באוגוסט", "בספטמבר", "באוקטובר", "בנובמבר", "בדצמבר"]

ACTIVITY_NAMES = {
    "running": "ריצה", "trail_running": "ריצת שטח", "treadmill_running": "ריצת הליכון",
    "walking": "הליכה", "hiking": "טיול", "cycling": "אופניים",
    "road_biking": "אופני כביש", "indoor_cycling": "אופני כושר",
    "strength_training": "כוח", "lap_swimming": "שחייה", "open_water_swimming": "שחייה בים",
    "yoga": "יוגה", "cardio": "אירובי", "elliptical": "אליפטיקל", "rowing": "חתירה",
}
SLEEP_QUALITY = {"EXCELLENT": "מצוין", "GOOD": "טוב", "FAIR": "סביר", "POOR": "גרוע"}
# Garmin's own ladder, confirmed against live responses: POOR sits below LOW.
READINESS_LEVEL = {
    "MAXIMUM": "מקסימלית", "HIGH": "גבוהה", "MODERATE": "בינונית",
    "LOW": "נמוכה", "POOR": "ירודה", "VERY_POOR": "ירודה מאוד", "NONE": "אין נתון",
}


def _n(value):
    """None-safe number: returns None instead of raising or printing 'None'."""
    return value if isinstance(value, (int, float)) else None


def _pace(seconds: float | None, meters: float | None) -> str | None:
    if not seconds or not meters or meters < 100:
        return None
    per_km = seconds / (meters / 1000)
    return f"{int(per_km // 60)}:{int(per_km % 60):02d}"


def _hhmm(hours: float | None) -> str | None:
    if not hours:
        return None
    return f"{int(hours)}:{round((hours % 1) * 60):02d}"


def _safe(fn, *args, **kwargs):
    """Garmin gaps are normal (no watch, no sync, a metric off). Never fatal."""
    try:
        return garmin._unwrap(fn(*args, **kwargs))
    except Exception as exc:
        print(f"  (skipped {getattr(fn, '__name__', fn)}: {type(exc).__name__})", file=sys.stderr)
        return None


def collect(date: str) -> dict:
    return {
        "activities": _safe(garmin.raw, "get_activities_by_date", date, date) or [],
        "summary": _safe(garmin.call, "get_user_summary", date) or {},
        "sleep": _safe(garmin.call, "get_sleep_summary", date) or {},
        "readiness": _safe(garmin.call, "get_training_readiness", date) or [],
        "status": _safe(garmin.call, "get_training_status", date) or {},
    }


def render(date: str, data: dict) -> tuple[str, bool]:
    """Returns (message, had_anything_to_say)."""
    d = dt.date.fromisoformat(date)
    lines = [f"סיכום יום — יום {HEB_DAYS[d.weekday()]}, {d.day} {HEB_MONTHS[d.month - 1]}", ""]
    substantive = False

    acts = data["activities"]
    if acts:
        substantive = True
        lines.append("אימונים")
        for a in acts:
            kind = ACTIVITY_NAMES.get((a.get("activityType") or {}).get("typeKey"), "אימון")
            bits = []
            meters, seconds = _n(a.get("distance")), _n(a.get("duration"))
            if meters and meters > 100:
                bits.append(f'{meters / 1000:.1f} ק"מ')
            if seconds:
                bits.append(f"{round(seconds / 60)} דק׳")
            pace = _pace(seconds, meters)
            if pace:
                bits.append(f'{pace} לק"מ')
            if _n(a.get("averageHR")):
                bits.append(f"דופק {round(a['averageHR'])}")
            if _n(a.get("activityTrainingLoad")):
                bits.append(f"עומס {round(a['activityTrainingLoad'])}")
            lines.append(f"• {kind} · " + " · ".join(bits) if bits else f"• {kind}")
        lines.append("")
    else:
        lines += ["אימונים", "• אין אימון רשום היום", ""]

    s = data["summary"]
    day = []
    if _n(s.get("totalSteps")):
        day.append(f"צעדים {s['totalSteps']:,}")
    if _n(s.get("totalKilocalories")):
        day.append(f"קלוריות {round(s['totalKilocalories']):,}")
    mod, vig = _n(s.get("moderateIntensityMinutes")) or 0, _n(s.get("vigorousIntensityMinutes")) or 0
    if mod or vig:
        day.append(f"דקות עצימות {mod + 2 * vig}")
    if _n(s.get("averageStressLevel")) and s["averageStressLevel"] > 0:
        day.append(f"סטרס ממוצע {round(s['averageStressLevel'])}")
    if day:
        substantive = True
        lines += ["היום", " · ".join(day), ""]

    sl = data["sleep"]
    if _n(sl.get("sleep_hours")):
        substantive = True
        parts = [f"{_hhmm(sl['sleep_hours'])} שעות"]
        if _n(sl.get("sleep_score")):
            q = SLEEP_QUALITY.get(sl.get("sleep_score_qualifier"), "")
            parts.append(f"ציון {sl['sleep_score']}" + (f" ({q})" if q else ""))
        if _n(sl.get("deep_sleep_percent")):
            parts.append(f"עמוק {sl['deep_sleep_percent']:.0f}%")
        if _n(sl.get("avg_overnight_hrv")):
            parts.append(f"HRV {round(sl['avg_overnight_hrv'])}")
        lines += ["שינה", " · ".join(parts), ""]
    else:
        lines += ["שינה", "אין נתוני שינה ללילה הזה", ""]

    # The morning reading is the one Garmin computes after waking, not a later refresh.
    morning = next((r for r in data["readiness"] if r.get("context") == "AFTER_WAKEUP_RESET"),
                   data["readiness"][0] if data["readiness"] else None)
    if morning and _n(morning.get("score")):
        substantive = True
        level = READINESS_LEVEL.get(morning.get("level"), morning.get("level", ""))
        lines.append(f"מוכנות הבוקר: {morning['score']} מתוך 100 — {level}")
        weak = _weakest_factor(morning)
        if weak:
            lines.append(f"הגורם החלש: {weak}")
        lines.append("")

    st = data["status"]
    if _n(st.get("acute_load")) and _n(st.get("chronic_load")):
        acwr = {"OPTIMAL": "אופטימלי", "LOW": "נמוך", "HIGH": "גבוה"}.get(
            st.get("acwr_status"), st.get("acwr_status") or "")
        ratio = st.get("load_ratio")
        lines.append(f"עומס: אקוטי {round(st['acute_load'])} / כרוני {round(st['chronic_load'])}"
                     + (f" · יחס {ratio}" if ratio else "") + (f" ({acwr})" if acwr else ""))
        if _n(st.get("vo2_max_precise")):
            lines.append(f"VO2max {st['vo2_max_precise']}")

    return "\n".join(lines).rstrip(), substantive


FACTOR_NAMES = {
    "sleep_factor_percent": "שינה",
    "sleep_history_factor_percent": "היסטוריית שינה",
    "recovery_factor_percent": "התאוששות",
    "hrv_factor_percent": "HRV",
    "stress_history_factor_percent": "היסטוריית סטרס",
    "training_load_factor_percent": "עומס אימון",
}


def _weakest_factor(readiness: dict) -> str | None:
    """Readiness is a single number hiding six. Name the one dragging it down."""
    scored = [(v, FACTOR_NAMES[k]) for k, v in readiness.items()
              if k in FACTOR_NAMES and isinstance(v, (int, float)) and v > 0]
    if not scored:
        return None
    value, name = min(scored)
    return f"{name} {round(value)}%" if value < 60 else None


# --------------------------------------------------------------------------
def _load_env_file(path: str) -> dict:
    cfg = {}
    try:
        with open(os.path.expanduser(path)) as fh:
            for line in fh:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return cfg


def telegram_config() -> tuple[str, str]:
    cfg = {}
    for path in ("~/.garmin-digest.env", "~/.claude/remote-bot.env"):
        for k, v in _load_env_file(path).items():
            cfg.setdefault(k, v)
    for token_key, chat_key in (("GARMIN_DIGEST_BOT_TOKEN", "GARMIN_DIGEST_CHAT_ID"),
                                ("REMOTE_BOT_TOKEN", "REMOTE_CHAT_ID")):
        token = os.getenv(token_key) or cfg.get(token_key)
        chat = os.getenv(chat_key) or cfg.get(chat_key)
        if token and chat:
            return token, chat
    raise SystemExit(
        "No Telegram credentials. Set GARMIN_DIGEST_BOT_TOKEN + GARMIN_DIGEST_CHAT_ID "
        "in the environment or in ~/.garmin-digest.env"
    )


TG_NOTIFY = os.path.expanduser("~/.claude/hooks/tg-notify.js")


def send(text: str) -> None:
    """Prefer the machine's own Telegram helper (it owns channel routing and
    retries); fall back to a direct API call so this works on a bare host."""
    if os.path.isfile(TG_NOTIFY):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node:
            result = subprocess.run([node, TG_NOTIFY, text, "--he"],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                print("sent → tg-notify", file=sys.stderr)
                return
            print(f"tg-notify failed ({result.returncode}): "
                  f"{result.stderr.strip()[:200]} — falling back", file=sys.stderr)

    token, chat = telegram_config()
    payload = json.dumps({"chat_id": chat, "text": text,
                          "disable_web_page_preview": True}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if not body.get("ok"):
        raise SystemExit(f"Telegram refused the message: {body.get('description')}")
    print("sent → Telegram", file=sys.stderr)


def _find_claude_exe() -> str | None:
    """Locate the Claude Code CLI bundled with the desktop app.

    The app auto-updates and re-versions its install folder, so this globs
    for whatever version is currently there instead of pinning one.
    """
    override = os.getenv("CLAUDE_EXE")
    if override and os.path.isfile(override):
        return override
    pattern = os.path.expanduser(r"~\AppData\Roaming\Claude\claude-code\*\claude.exe")
    candidates = glob.glob(pattern)
    return max(candidates, key=os.path.getmtime) if candidates else None


AI_PROMPT = """הנה סיכום יומי של נתוני גרמין שלי (צעדים, אימונים, שינה, מוכנות, עומס אימון). \
תוסיף בעברית סעיף קצר בשם "ניתוח והצעות לשיפור" (2-4 משפטים): תובנה אחת קונקרטית \
ו-1-3 המלצות פעולה, כולל המלצת אימון ליום הקרוב (סוג, עצימות, האם יום התאוששות) אם \
הנתונים תומכים בזה - הכל מבוסס אך ורק על המספרים שבסיכום, בלי להמציא נתונים שלא מופיעים \
בו. אם היום דל בנתונים, תגיד את זה בקצרה ואל תמציא. תחזיר רק את הסעיף הזה, בלי לחזור על \
הסיכום המקורי.

--- הסיכום ---
{digest}
"""


def ai_analysis(digest_text: str) -> str | None:
    """Ask the local Claude Code CLI for coaching-level suggestions.

    Best-effort: any failure (not installed, not logged in, timeout) just
    means the digest goes out without this section - never blocks sending.
    """
    exe = _find_claude_exe()
    if not exe:
        print("  (claude.exe not found; skipping AI analysis)", file=sys.stderr)
        return None
    try:
        result = subprocess.run(
            [exe, "-p", AI_PROMPT.format(digest=digest_text)],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
    except Exception as exc:
        print(f"  (AI analysis skipped: {type(exc).__name__})", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        print(f"  (AI analysis skipped: exit {result.returncode}: "
              f"{result.stderr.strip()[:200]})", file=sys.stderr)
        return None
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Garmin digest for Telegram")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--send", action="store_true", help="post to Telegram")
    parser.add_argument("--always", action="store_true",
                        help="send even when the day has nothing in it")
    parser.add_argument("--ai", action="store_true",
                        help="append Claude-generated analysis and suggestions "
                             "(needs the local Claude Code CLI logged in)")
    args = parser.parse_args()

    message, substantive = render(args.date, collect(args.date))
    print(message)

    if args.ai and substantive:
        analysis = ai_analysis(message)
        if analysis:
            message = message + "\n\n" + analysis
            print("\n" + analysis)

    if args.send:
        if substantive or args.always:
            send(message)
        else:
            print("nothing recorded for this day; not sending", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
