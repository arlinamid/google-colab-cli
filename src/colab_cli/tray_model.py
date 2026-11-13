# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""What the tray shows, without Qt (docs/09_tray_and_profiles.md).

collect_snapshot() gathers one picture of the account, the runtimes and
their jobs; the pure functions below turn it into the icon state, the cards,
the CU estimate and the notifications. `colab tray --snapshot` prints the
same snapshot.

Cost rule: the tray must never keep a runtime alive. It sends no keep-alive
and does not use the kernel. It reads a runtime's job files (Contents API)
on every poll only while a job runs there; a runtime without a running job is
looked at again every IDLE_RECHECK_SEC (to notice a job started from a
terminal), an orphan (no local session) never.
"""

import datetime
import time
from typing import Any, Dict, List, Optional, Set

from colab_cli.contents import ContentsClient
from colab_cli.jobs import (
    DEFAULT_STALL_AFTER,
    JOBS_DIR,
    RUNNING_STATES,
    job_record,
    parse_metrics,
)
from colab_cli.results import SCHEMA

ACCOUNT_EVERY_SEC = 300.0
IDLE_RECHECK_SEC = 300.0
LOW_BALANCE_HOURS = 1.0
# Colab reclaims an idle runtime after about 90 minutes.
IDLE_WARN_SEC = 75 * 60
TERMINAL_STATES = ("succeeded", "failed", "stopped")

COLORS = {
    "none": "#9aa0a6",
    "idle": "#34a853",
    "running": "#4285f4",
    "warning": "#f9ab00",
    "error": "#ea4335",
    "stale": "#9aa0a6",
}


def _hardware(accelerator: Optional[str]) -> str:
    return "CPU" if not accelerator or accelerator == "NONE" else accelerator


def _job_ids(files) -> List[str]:
    try:
        listing = files.list_dir(JOBS_DIR)
    except FileNotFoundError:
        return []
    ids = [i["name"] for i in listing.get("content", []) if i.get("type") == "directory"]
    return sorted(ids, key=lambda i: (i.split("-")[-3:-1], i))


def _read_job(files, job_id: str, session: str, now: float) -> Optional[Dict[str, Any]]:
    import json

    base = f"{JOBS_DIR}/{job_id}"
    try:
        status = json.loads(files.read_text(f"{base}/status.json"))
    except (FileNotFoundError, ValueError):
        return None

    def optional(name):
        try:
            return files.read_text(f"{base}/{name}")
        except FileNotFoundError:
            return ""

    rec = job_record(
        status, metrics=parse_metrics(optional("metrics.jsonl")), tail=optional("tail.txt"),
        now=now, stall_after=DEFAULT_STALL_AFTER, session=session, tail_lines=5,
    )
    rec.pop("schema", None)
    return rec


def _parse_local_time(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _session_entry(s, assignment, prev_entry, now: float) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "name": s.name if s else "?",
        "endpoint": assignment.endpoint,
        "hardware": _hardware(assignment.accelerator.value),
        "local": s is not None,
        "profile": getattr(s, "profile", None) if s else None,
        "stop_when_done": getattr(s, "stop_when_done", None) if s else None,
        "running": getattr(s, "running", None) if s else None,
        "activity": getattr(s, "activity", None) if s else None,
        "last_execution_at": _parse_local_time(s.last_execution[2]) if s and s.last_execution else None,
        "job": (prev_entry or {}).get("job"),
        "jobs_checked_at": (prev_entry or {}).get("jobs_checked_at"),
    }
    if s is None:
        return entry
    job = entry["job"]
    running = bool(job and job["state"] in RUNNING_STATES)
    checked = entry["jobs_checked_at"]
    if running or checked is None or now - checked >= IDLE_RECHECK_SEC:
        files = ContentsClient(s)
        ids = _job_ids(files)
        entry["jobs_checked_at"] = now
        if ids:
            newest = ids[-1]
            same_finished = job and job["id"] == newest and job["state"] in TERMINAL_STATES
            if not same_finished:
                entry["job"] = _read_job(files, newest, s.name, now) or job
        else:
            entry["job"] = None
    return entry


def collect_snapshot(state, prev: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """One picture of the account, the runtimes and their newest jobs.

    On a failed refresh the previous data is kept, with ``ok`` False and the
    error; ``last_ok_at`` says how old the data is.
    """
    now = time.time() if now is None else now
    prev = prev or {}
    snap: Dict[str, Any] = {
        "schema": SCHEMA,
        "type": "tray_snapshot",
        "taken_at": now,
        "last_ok_at": prev.get("last_ok_at"),
        "ok": True,
        "error": None,
        "account": prev.get("account"),
        "sessions": prev.get("sessions", []),
    }
    try:
        # State caches the local sessions for the length of one command; the
        # tray runs for hours, so read sessions.json again on every refresh
        # (a session created after the tray started is not an orphan).
        if hasattr(state, "_sessions"):
            state._sessions = None
        sessions, assignments = state.sync_sessions()
        by_endpoint = {s.endpoint: s for s in sessions.values()}
        prev_by_endpoint = {e["endpoint"]: e for e in prev.get("sessions", [])}
        entries = []
        for a in assignments:
            entries.append(_session_entry(by_endpoint.get(a.endpoint), a, prev_by_endpoint.get(a.endpoint), now))
        account = snap["account"]
        if account is None or now - account.get("fetched_at", 0) >= ACCOUNT_EVERY_SEC:
            info = state.client.get_consumption_user_info()
            account = {
                "balance_cu": info.paid_compute_units_balance,
                "rate_cu_per_hour": info.consumption_rate_hourly,
                "assignments": info.assignments_count,
                "fetched_at": now,
            }
        snap.update(sessions=entries, account=account, last_ok_at=now)
    except Exception as e:  # shown on the panel; the last data stays
        snap.update(ok=False, error=f"{type(e).__name__}: {e}")
    return snap


# --- pure functions for the panel ---

def card_model(entry: Dict[str, Any], now: float, services: Optional[Set[str]] = None) -> Dict[str, Any]:
    """One runtime's card: state, main metric, progress or last output."""
    job = entry.get("job")
    activity = entry.get("activity") if entry.get("running") else None
    card: Dict[str, Any] = {
        "name": entry["name"],
        "hardware": entry["hardware"],
        "local": entry["local"],
        "profile": entry.get("profile"),
        "state": "idle",
        "job_id": None,
        "can_stop_job": False,
        "percent": None,
        "metric": None,
        "epoch": None,
        "cell": None,
        "eta_s": None,
        "last_output_age_s": None,
        "diagnosis": None,
    }
    if job:
        state = job["state"]
        card["job_id"] = job["id"]
        card["state"] = {
            "succeeded": "done", "failed": "failed", "stopped": "stopped",
        }.get(state, "stalled" if job.get("stalled") else "running")
        card["can_stop_job"] = state in RUNNING_STATES
        card["percent"] = job.get("overall_percent")
        card["last_output_age_s"] = job.get("last_output_age_s")
        card["eta_s"] = (job.get("progress") or {}).get("eta_s") if card["can_stop_job"] else None
        if job.get("epoch"):
            card["epoch"] = (job["epoch"]["current"], job["epoch"]["total"])
        if job.get("cell"):
            c = job["cell"]
            card["cell"] = (c["index"], c["total"], c["label"])
        card["metric"] = _main_metric(job.get("metrics") or {})
        if job.get("diagnosis"):
            card["diagnosis"] = job["diagnosis"].get("code")
    if activity and card["state"] not in ("running", "stalled"):
        card["state"] = "running"
        total = activity.get("total") or 0
        card["cell"] = (activity.get("cell"), total, activity.get("label"))
        if total:
            card["percent"] = round(100 * (activity.get("cell", 1) - 1) / total, 1)
    if card["state"] == "idle" and services and entry["name"] in services:
        card["state"] = "service"
    return card


def _main_metric(metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    trend = metrics.get("trend") or {}
    if not trend:
        return None
    name = "loss" if "loss" in trend else next(iter(trend))
    t = trend[name]
    direction = None
    if t["last"] < t["first"]:
        direction = "down"
    elif t["last"] > t["first"]:
        direction = "up"
    return {"name": name, "value": t["last"], "trend": direction}


def cu_model(account: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Balance, rate and the hours left at that rate (always an estimate)."""
    if not account:
        return None
    rate = account.get("rate_cu_per_hour") or 0
    balance = account.get("balance_cu")
    hours = round(balance / rate, 1) if rate > 0 and balance is not None else None
    return {"balance_cu": balance, "rate_cu_per_hour": rate, "hours_left": hours, "estimate": True}


def _idle_for(entry: Dict[str, Any], card: Dict[str, Any], now: float) -> Optional[float]:
    if card["state"] not in ("idle", "done", "failed", "stopped") or not entry["local"]:
        return None
    last = entry.get("last_execution_at")
    job = entry.get("job") or {}
    ended = job.get("ended_at")
    if ended:
        try:
            last = max(last or 0, datetime.datetime.fromisoformat(ended).timestamp())
        except ValueError:
            pass
    return now - last if last else None


def icon_state(snap: Dict[str, Any], now: float, interval: float, services: Optional[Set[str]] = None) -> Dict[str, Any]:
    """The tray icon's level, colour and a text that says the same in words.

    Levels, worst first: stale (no successful refresh for two intervals),
    error (refresh failed, failed job), warning (stalled job, low balance,
    a runtime idle for long), running, idle, none.
    """
    cards = [card_model(e, now, services) for e in snap.get("sessions", [])]
    reasons: List[str] = []
    codes: List[Dict[str, Any]] = []
    last_ok = snap.get("last_ok_at")
    if last_ok is not None and now - last_ok > 2 * interval:
        minutes = int((now - last_ok) // 60)
        level, text = "stale", f"Stale data (last refresh {minutes} min ago)"
        codes.append({"code": "stale", "minutes": minutes})
    elif not snap.get("ok"):
        level, text = "error", f"Refresh failed: {snap.get('error')}"
        codes.append({"code": "refresh_failed", "error": snap.get("error")})
    else:
        failed = [c for c in cards if c["state"] == "failed"]
        stalled = [c for c in cards if c["state"] == "stalled"]
        cu = cu_model(snap.get("account"))
        low = cu and cu["hours_left"] is not None and cu["hours_left"] < LOW_BALANCE_HOURS
        idle_long = [
            e["name"] for e, c in zip(snap.get("sessions", []), cards)
            if (_idle_for(e, c, now) or 0) > IDLE_WARN_SEC
        ]
        if failed:
            level = "error"
            reasons.append("job failed on " + ", ".join(c["name"] for c in failed))
            codes.append({"code": "job_failed", "names": [c["name"] for c in failed]})
        elif stalled or low or idle_long:
            level = "warning"
            if stalled:
                reasons.append("no output from " + ", ".join(c["name"] for c in stalled))
                codes.append({"code": "stalled", "names": [c["name"] for c in stalled]})
            if low:
                reasons.append(f"low balance (about {cu['hours_left']} h left)")
                codes.append({"code": "low_balance", "hours": cu["hours_left"]})
            if idle_long:
                reasons.append("idle for long: " + ", ".join(idle_long))
                codes.append({"code": "idle_long", "names": idle_long})
        elif any(c["state"] in ("running", "service") for c in cards):
            level = "running"
        elif cards:
            level = "idle"
        else:
            level = "none"
        text = summary_text(cards) + ("; " + "; ".join(reasons) if reasons else "")
    return {"level": level, "color": COLORS[level], "text": text, "reasons": codes}


def summary_text(cards: List[Dict[str, Any]]) -> str:
    if not cards:
        return "No runtimes"
    parts = []
    for c in cards:
        part = f"{c['name']} {c['hardware']} {c['state']}"
        if c["percent"] is not None and c["state"] in ("running", "stalled"):
            part += f" {c['percent']:g}%"
        parts.append(part)
    return f"{len(cards)} runtime(s): " + ", ".join(parts)


def events(prev: Optional[Dict[str, Any]], cur: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What changed between two snapshots that deserves a notification."""
    if not prev or not cur.get("ok"):
        return []
    out: List[Dict[str, Any]] = []
    prev_by = {e["endpoint"]: e for e in prev.get("sessions", [])}
    cur_by = {e["endpoint"]: e for e in cur.get("sessions", [])}
    for endpoint, e in cur_by.items():
        old_job = (prev_by.get(endpoint) or {}).get("job")
        job = e.get("job")
        if not job or not old_job or old_job["id"] != job["id"]:
            continue
        if old_job["state"] in RUNNING_STATES and job["state"] in TERMINAL_STATES:
            kind = {"succeeded": "job_done", "failed": "job_failed"}.get(job["state"], "job_stopped")
            if job["state"] == "stopped" and job.get("stop_reason") == "budget":
                kind = "budget"
            out.append({
                "kind": kind, "session": e["name"], "job": job["id"],
                "code": (job.get("diagnosis") or {}).get("code"),
            })
    for endpoint, e in prev_by.items():
        if endpoint not in cur_by and e["local"]:
            out.append({"kind": "session_gone", "session": e["name"], "job": None, "code": None})
    old_cu, new_cu = cu_model(prev.get("account")), cu_model(cur.get("account"))
    if (new_cu and new_cu["hours_left"] is not None and new_cu["hours_left"] < LOW_BALANCE_HOURS
            and not (old_cu and old_cu["hours_left"] is not None and old_cu["hours_left"] < LOW_BALANCE_HOURS)):
        out.append({"kind": "low_balance", "session": None, "job": None, "code": None})
    return out


def sessions_to_stop(snap: Dict[str, Any], already: Set[str]) -> List[str]:
    """Profile runtimes whose job has ended and that should stop (stop_when_done)."""
    names = []
    for e in snap.get("sessions", []):
        job = e.get("job")
        if (e["local"] and e.get("profile") and e.get("stop_when_done") and job
                and job["state"] in TERMINAL_STATES and e["name"] not in already):
            names.append(e["name"])
    return names
