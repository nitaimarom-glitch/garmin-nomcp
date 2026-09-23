#!/usr/bin/env python3
"""Pull ~60 days of Garmin trend data + yesterday's digest, push to the
private garmin-secrets repo as JSON. A cloud routine reads that file to do
the actual coaching analysis (Garmin blocks API calls from datacenter IPs,
so the live pull has to happen from this machine).

    ./push_trend_data.py                 pull + push
    ./push_trend_data.py --dry-run        pull + print, skip git push

Never talks to Telegram or Claude - purely a data mule. Designed to run
unattended from Task Scheduler: any single failed sub-call is skipped
(logged to stderr), never crashes the whole run.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import garmin  # noqa: E402
from daily_digest import collect, render  # noqa: E402

SECRETS_REPO = os.path.expanduser("~/garmin-secrets-local")
TREND_DAYS = 60
LOG_PATH = os.path.expanduser("~/garmin-sync.log")
STATUS_FILE = "sync_status.json"


def log(message: str) -> None:
    """Append to a local log AND echo to stderr.

    Unattended from Task Scheduler nothing captures stderr, so without this
    a failure leaves no trace at all - which is exactly what happened when a
    hung login got killed at the 10-minute limit: no data, no push, and no
    recorded reason. The log is written line-by-line (flushed immediately)
    so even a run that gets killed mid-flight leaves its last known step.
    """
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    line = f"{stamp}  {message}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
    except OSError:
        pass


def write_status(ok: bool, detail: str, generated_at: str | None = None) -> None:
    """Record the outcome of this run where the cloud routine can read it.

    Pushed to the secrets repo alongside trend_data.json, so when the data is
    stale the email can say *why* instead of just noting it is old.
    """
    payload = {
        "attempted_at": dt.datetime.now().isoformat(timespec="seconds"),
        "ok": ok,
        "detail": detail,
        "data_generated_at": generated_at,
    }
    try:
        with open(os.path.join(SECRETS_REPO, STATUS_FILE), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"could not write {STATUS_FILE}: {exc}")


def _safe_call(fn, *args, label=""):
    try:
        return fn(*args)
    except Exception as exc:
        log(f"  skipped {label or getattr(fn, '__name__', fn)}: "
            f"{type(exc).__name__}: {exc}")
        return None


def _pace_per_km(distance_m, duration_s):
    if not distance_m or not duration_s or distance_m < 100:
        return None
    per_km = duration_s / (distance_m / 1000)
    return f"{int(per_km // 60)}:{int(per_km % 60):02d}"


def _pace_per_100m(distance_m, duration_s):
    if not distance_m or not duration_s or distance_m < 50:
        return None
    per_100 = duration_s / (distance_m / 100)
    return f"{int(per_100 // 60)}:{int(per_100 % 60):02d}"


def _summarize_activity(a: dict) -> dict:
    kind = (a.get("activityType") or {}).get("typeKey", "unknown")
    distance = a.get("distance")
    duration = a.get("duration")
    start_local = a.get("startTimeLocal")
    end_local = None
    if start_local and duration:
        try:
            end_dt = (dt.datetime.strptime(start_local, "%Y-%m-%d %H:%M:%S")
                      + dt.timedelta(seconds=duration))
            end_local = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    out = {
        # Garmin buckets an activity under its START date - a run starting
        # 23:30 and finishing after midnight is filed under the earlier day
        # even though it ends on the next one. `date` mirrors that bucket
        # (for continuity/back-compat), but `start_local`/`end_local` carry
        # the real timestamps so a consumer can correctly place an activity
        # that crosses midnight into the day it actually finished in.
        "date": (start_local or "")[:10],
        "start_local": start_local,
        "end_local": end_local,
        "type": kind,
        "name": a.get("activityName"),
        "distance_km": round(distance / 1000, 2) if distance else None,
        "duration_min": round(duration / 60, 1) if duration else None,
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "training_load": a.get("activityTrainingLoad"),
        "aerobic_effect": a.get("aerobicTrainingEffect"),
        "anaerobic_effect": a.get("anaerobicTrainingEffect"),
        "calories": a.get("calories"),
    }
    if "swim" in kind:
        out["pace_per_100m"] = _pace_per_100m(distance, duration)
        out["swolf"] = a.get("averageSwolf")
        out["stroke_rate_spm"] = a.get("averageSwimCadenceInStrokesPerMinute")
    elif kind in ("running", "trail_running", "treadmill_running"):
        out["pace_per_km"] = _pace_per_km(distance, duration)
        out["cadence_spm"] = a.get("averageRunningCadenceInStepsPerMinute")
        out["elevation_gain_m"] = a.get("elevationGain")
    return {k: v for k, v in out.items() if v is not None}


def build_payload() -> dict:
    # The email goes out at 23:00, same day - so "the day being summarized" is
    # today, not yesterday. Sleep (last night) and any activity already done
    # today are both available by then.
    today = dt.date.today()
    start = today - dt.timedelta(days=TREND_DAYS)

    digest_text, substantive = render(today.isoformat(), collect(today.isoformat()))

    raw_activities = _safe_call(
        lambda: garmin.raw("get_activities_by_date", start.isoformat(), today.isoformat()),
        label="get_activities_by_date") or []
    activities = [_summarize_activity(a) for a in raw_activities]
    activities.sort(key=lambda a: a.get("date", ""))

    vo2max_trend = _safe_call(
        lambda: garmin.call("get_vo2max_trend", start.isoformat(), today.isoformat()),
        label="get_vo2max_trend")
    training_load_trend = _safe_call(
        lambda: garmin.call("get_training_load_trend", start.isoformat(), today.isoformat()),
        label="get_training_load_trend")
    race_predictions = _safe_call(
        lambda: garmin.call("get_race_predictions"), label="get_race_predictions")

    def _maybe_parse(x):
        if isinstance(x, str):
            try:
                return json.loads(x)
            except (ValueError, TypeError):
                return x
        return x

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "today_date": today.isoformat(),
        "today_digest": digest_text,
        "today_had_data": substantive,
        "trend_window_days": TREND_DAYS,
        "activities": activities,
        "vo2max_trend": _maybe_parse(vo2max_trend),
        "training_load_trend": _maybe_parse(training_load_trend),
        "race_predictions": _maybe_parse(race_predictions),
    }


def _compact_activity(a: dict) -> dict:
    keep = ("date", "type", "distance_km", "duration_min", "avg_hr",
            "pace_per_km", "cadence_spm", "pace_per_100m", "swolf", "stroke_rate_spm")
    out = {k: (round(a[k]) if k in ("avg_hr", "cadence_spm", "stroke_rate_spm") else a[k])
           for k in keep if a.get(k) is not None}
    if a.get("training_load") is not None:
        out["load"] = round(a["training_load"])
    if a.get("end_local"):
        out["ended"] = a["end_local"][:16]
    return out


def build_brief(payload: dict) -> dict:
    """Small, pre-digested input for the cloud routines.

    The routines are billed per token and per turn. Feeding them the raw
    ~46K-char trend_data.json made them spend a dozen-plus turns exploring
    it with ad-hoc scripts. Everything deterministic (the cross-midnight
    fix, what happened since Saturday, trend direction) is computed here
    for free, so a routine can read one ~4K file and go straight to writing.
    """
    today = dt.date.fromisoformat(payload["today_date"])
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    # Weekly plans are sent Saturday night and cover Sat..Fri.
    saturday = (today - dt.timedelta(days=(today.weekday() - 5) % 7)).isoformat()
    acts = payload["activities"]

    # Garmin files an activity under its START date, so one that starts
    # before midnight and ends after it is dated yesterday - missed by
    # yesterday's email (sent before it began) and by today's digest.
    late_from_yesterday = [_compact_activity(a) for a in acts
                           if a.get("date") == yesterday
                           and (a.get("end_local") or "")[:10] == today.isoformat()]

    since_saturday = [_compact_activity(a) for a in acts
                      if (a.get("end_local") or a.get("date") or "")[:10] >= saturday]

    swims = [_compact_activity(a) for a in acts if "swim" in a.get("type", "")][-8:]
    runs = [_compact_activity(a) for a in acts if "running" in a.get("type", "")][-8:]

    load = (payload.get("training_load_trend") or {})
    load_rows = load.get("trend", []) if isinstance(load, dict) else []
    load_14d = [{k: r.get(k) for k in ("date", "atl", "ctl", "tsb", "acwr", "acwr_status")}
                for r in load_rows[-14:]]

    vo2 = payload.get("vo2max_trend") or {}
    race = (payload.get("race_predictions") or {})
    race = race.get("predictions", race) if isinstance(race, dict) else race
    if isinstance(race, dict):
        race = {k: (v.get("time") if isinstance(v, dict) else v) for k, v in race.items()}

    return {
        "today_date": payload["today_date"],
        "generated_at": payload["generated_at"],
        "week_started": saturday,
        "today_digest": payload["today_digest"],
        "late_activities_from_yesterday_count_for_today": late_from_yesterday,
        "activities_since_week_start": since_saturday,
        "recent_swims": swims,
        "recent_runs": runs,
        "load_last_14d": load_14d,
        "vo2max": {"latest": vo2.get("latest_vo2_max"), "change_60d": vo2.get("change")}
        if isinstance(vo2, dict) else None,
        "race_predictions": race,
    }


def _looks_empty(payload: dict) -> bool:
    """A legitimate rest day still has sleep/summary/readiness data - a
    watch doesn't stop reporting just because no workout happened. If
    EVERY category came back empty, that's a connection/auth failure
    silently swallowed by _safe_call/_safe, not a quiet day."""
    if payload["today_had_data"]:
        return False
    if payload["activities"]:
        return False
    if (payload["vo2max_trend"] or payload["training_load_trend"]
            or payload["race_predictions"]):
        return False
    return True


def build_payload_with_retries(attempts: int = 3) -> dict:
    """Retry the whole pull a few times before giving up for the day.

    Login failures are already retried inside garmin.connect(), but this
    covers everything else transient (a mid-pull disconnect, a stalled
    request) and - more importantly - catches the case where every
    individual call "succeeded" by returning nothing, because the client
    was silently broken the whole time.
    """
    last_exc = None
    for attempt in range(attempts):
        try:
            payload = build_payload()
        except (Exception, SystemExit) as exc:
            last_exc = exc
            log(f"  build attempt {attempt + 1}/{attempts} failed: "
                f"{type(exc).__name__}: {exc}")
        else:
            if not _looks_empty(payload):
                return payload
            log(f"  build attempt {attempt + 1}/{attempts} came back completely "
                f"empty - treating as a failed pull, not a rest day")
        if attempt < attempts - 1:
            garmin.reset()  # force a fresh login on the next attempt
            time.sleep(30)
    raise RuntimeError(
        f"could not get real Garmin data after {attempts} attempts"
        + (f" (last error: {type(last_exc).__name__}: {last_exc})"
           if last_exc else " (kept coming back empty)")
    )


def _git_push(commit_message: str) -> int:
    """Commit + push whatever is currently in the secrets repo.

    Deliberately independent of whether the Garmin pull succeeded: when it
    fails, the status file explaining WHY still needs to reach the cloud
    routine, and pushing it doesn't involve Garmin at all.
    """
    # GIT_TERMINAL_PROMPT=0 makes git fail fast instead of hanging on a
    # credential prompt that can never be answered under Task Scheduler
    # with a locked screen - that silent hang is what broke this pipeline
    # for two days (SCHED_E_ALREADY_RUNNING blocking every run after it).
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def run(cmd):
        try:
            return subprocess.run(cmd, cwd=SECRETS_REPO, capture_output=True,
                                   text=True, env=git_env, timeout=30)
        except subprocess.TimeoutExpired as exc:
            log(f"{' '.join(cmd)} timed out after 30s")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(exc))

    run(["git", "add", "-A"])
    if run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        log("no changes to push")
        return 0

    commit = run(["git", "-c", "user.email=nitaimarom@gmail.com", "-c", "user.name=Nitay",
                  "commit", "-m", commit_message])
    if commit.returncode != 0:
        log(f"git commit failed: {commit.stderr.strip()}")
        return 1

    push_result = run(["git", "push"])
    if push_result.returncode != 0:
        log(f"git push failed: {push_result.stderr.strip()}")
        return 1

    log("pushed to garmin-secrets")
    return 0


def push(dry_run: bool = False) -> int:
    log("=== sync run started ===")
    try:
        payload = build_payload_with_retries()
    except (Exception, SystemExit) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log(f"FAILED to get Garmin data - {reason}")
        # Record the reason and push it even though the data pull failed, so
        # tonight's email can state the cause instead of silently analysing
        # yesterday's numbers.
        write_status(False, reason)
        if not dry_run:
            _git_push(f"sync failed {dt.datetime.now().isoformat(timespec='seconds')}")
        return 1

    out_path = os.path.join(SECRETS_REPO, "trend_data.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log(f"wrote trend_data.json ({len(payload['activities'])} activities in "
        f"last {TREND_DAYS}d)")

    with open(os.path.join(SECRETS_REPO, "brief.json"), "w", encoding="utf-8") as fh:
        json.dump(build_brief(payload), fh, ensure_ascii=False, separators=(",", ":"))

    # Keep the mirrored Garmin token fresh too (garth rewrites it in place
    # on refresh, so the cloud copy can silently go stale otherwise).
    token_src = os.path.expanduser("~/.garminconnect/garmin_tokens.json")
    token_dst = os.path.join(SECRETS_REPO, "garminconnect-tokens", "garmin_tokens.json")
    if os.path.isfile(token_src):
        with open(token_src, "rb") as f_in, open(token_dst, "wb") as f_out:
            f_out.write(f_in.read())

    write_status(True, "sync ok", payload["generated_at"])

    if dry_run:
        log("--dry-run: not pushing to git")
        return 0

    return _git_push(f"trend data {payload['generated_at']}")


if __name__ == "__main__":
    sys.exit(push(dry_run="--dry-run" in sys.argv))
