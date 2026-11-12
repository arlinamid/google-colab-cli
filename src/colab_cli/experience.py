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

"""Learning from the local history (docs/08_agent_feedback.md, phase E).

Reads the per-session event logs (`history.py`) and answers: has this error
happened before, what fixed it then, and how long and costly is a run of
this file usually. Events logged before phase E carry only code and outputs;
they are re-diagnosed from those outputs.
"""

import datetime
import os
import re
import statistics
from typing import Any, Dict, List, Optional

from colab_cli.diagnosis import diagnose
from colab_cli.results import strip_ansi

FIX_WINDOW = datetime.timedelta(hours=24)

_SINCE_RE = re.compile(r"^(\d+)([mhdw])$")
_SINCE_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_PACKAGES_RE = re.compile(r"packages = \[(.*?)\]")
_PIP_LINE_RE = re.compile(r"^\s*[!%]\s*pip3?\s+install\s+(.+)$", re.MULTILINE)


def parse_since(text: str) -> datetime.timedelta:
    """'30m', '24h', '7d' or '2w' as a timedelta."""
    m = _SINCE_RE.match((text or "").strip())
    if not m:
        raise ValueError(f"Expected a duration like 30m, 24h, 7d or 2w, not {text!r}.")
    return datetime.timedelta(**{_SINCE_UNITS[m.group(2)]: int(m.group(1))})


def _when(event) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(event.get("timestamp", ""))
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def load_events(history, sessions: Optional[List[str]] = None, since: Optional[datetime.datetime] = None) -> List[Dict]:
    """All events of all (or the given) sessions, oldest first, with ``session``."""
    events = []
    for name in history.list_sessions():
        if sessions and name not in sessions:
            continue
        for event in history.get_history(name):
            event = dict(event, session=name)
            if since is not None and _when(event) < since:
                continue
            events.append(event)
    events.sort(key=_when)
    return events


def _where(location) -> Optional[str]:
    if not location or not location.get("file"):
        return None
    text = os.path.basename(location["file"].replace("\\", "/"))
    if location.get("cell"):
        text += f"#cell{location['cell']}"
    if location.get("line"):
        text += f":{location['line']}"
    return text


def execution_outcome(event) -> Optional[Dict[str, Any]]:
    """What an execution event says: status, error, diagnosis code, where."""
    if event.get("event_type") != "execution":
        return None
    if "status" in event:
        error = event.get("error") or {}
        outcome = {
            "status": event["status"],
            "ename": error.get("ename"),
            "evalue": error.get("evalue"),
            "diagnosis": event.get("diagnosis"),
            "where": _where(error.get("location")),
        }
    else:  # logged before phase E: work it out from the outputs
        errors = [o for o in event.get("outputs") or [] if o.get("output_type") == "error"]
        if event.get("via") == "run":
            errors = [
                e for e in errors
                if not (e.get("ename") == "SystemExit" and str(e.get("evalue")) in ("0", "None", ""))
            ]
        outcome = {"status": "ok", "ename": None, "evalue": None, "diagnosis": None, "where": None}
        if errors:
            e = errors[-1]
            tb = strip_ansi("\n".join(e.get("traceback") or []))
            outcome.update(
                status="error",
                ename=e.get("ename"),
                evalue=e.get("evalue"),
                diagnosis=diagnose(e.get("ename", ""), e.get("evalue", ""), tb)["code"],
            )
    outcome.update(
        file=event.get("file"),
        label=event.get("label"),
        duration_s=event.get("duration_s"),
        cost_cu=event.get("cost_cu"),
        code=event.get("code"),
        session=event.get("session"),
        timestamp=event.get("timestamp"),
    )
    return outcome


def _normalize(evalue: Optional[str]) -> str:
    # Only whole numbers (Errno 28, line 3, sizes): digits inside a name
    # such as 'py3dmol' or 'm0' keep two modules apart.
    text = re.sub(r"0x[0-9a-fA-F]+", "0x_", evalue or "")
    return re.sub(r"\b\d+(\.\d+)?\b", "N", text)[:160]


def signature(outcome) -> tuple:
    """What makes two failures 'the same': diagnosis, exception, message.

    Line numbers are left out on purpose: they move as the code is edited.
    """
    return (outcome.get("diagnosis"), outcome.get("ename"), _normalize(outcome.get("evalue")))


def _run_key(outcome) -> Optional[str]:
    name = outcome.get("file") or outcome.get("label")
    if not name:
        return None
    return os.path.basename(name.replace("\\", "/"))


def installs_in(event) -> List[str]:
    """Packages a `colab install` event or `!pip install` lines installed."""
    if event.get("event_type") == "automation" and event.get("op") == "install":
        m = _PACKAGES_RE.search(event.get("code") or "")
        return re.findall(r"'([^']*)'", m.group(1)) if m else []
    if event.get("event_type") == "execution":
        found = []
        for args in _PIP_LINE_RE.findall(event.get("code") or ""):
            found += [a for a in args.split() if not a.startswith("-")]
        return found
    return []


def find_fix(events: List[Dict], index: int) -> Optional[Dict[str, Any]]:
    """How the failure at ``events[index]`` was fixed, if it was.

    The fix is the first later successful run of the same file (any run for
    old events that name no file) in the same session within FIX_WINDOW.
    """
    failure = events[index]
    failed = execution_outcome(failure)
    key = _run_key(failed)
    start = _when(failure)
    session = failure.get("session")
    steps: List[Dict[str, Any]] = []
    attempts = 0
    for event in events[index + 1:]:
        if event.get("session") != session:
            continue
        if _when(event) - start > FIX_WINDOW:
            return None
        outcome = execution_outcome(event)
        same_run = outcome is not None and (key is None or _run_key(outcome) == key)
        if same_run and outcome["status"] == "ok":
            return {
                "after_s": round((_when(event) - start).total_seconds(), 1),
                "attempts": attempts,
                "code_changed": (outcome.get("code") or "") != (failed.get("code") or ""),
                "steps": steps,
            }
        if same_run:
            attempts += 1
        packages = installs_in(event)
        if packages:
            steps.append({"install": packages})
        if event.get("event_type") == "file_operation" and event.get("op") == "upload":
            steps.append({"upload": event.get("remote")})
    return None


def _failures(events):
    for i, event in enumerate(events):
        outcome = execution_outcome(event)
        if outcome and outcome["status"] == "error":
            yield i, outcome


def _last_fix(events, indexes):
    for i in sorted(indexes, reverse=True):
        fix = find_fix(events, i)
        if fix:
            return fix
    return None


def error_groups(events: List[Dict], limit: Optional[int] = 20) -> List[Dict[str, Any]]:
    """Past failures grouped by signature, most recently seen first."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    for i, outcome in _failures(events):
        g = groups.setdefault(signature(outcome), {
            "code": outcome["diagnosis"], "ename": outcome["ename"], "where": None,
            "evalue": None, "count": 0, "sessions": set(), "first_seen": outcome["timestamp"],
            "last_seen": None, "indexes": [],
        })
        g["count"] += 1
        g["sessions"].add(outcome["session"])
        g["last_seen"] = outcome["timestamp"]
        g["evalue"] = outcome["evalue"]
        g["where"] = outcome["where"] or g["where"]
        g["indexes"].append(i)
    rows = []
    for g in sorted(groups.values(), key=lambda g: g["last_seen"] or "", reverse=True)[:limit]:
        indexes = g.pop("indexes")
        g["sessions"] = sorted(s for s in g["sessions"] if s)
        g["last_fix"] = _last_fix(events, indexes)
        rows.append(g)
    return rows


def seen_before(events: List[Dict], outcome: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Earlier failures with the same signature, and how the last one was fixed."""
    target = signature(outcome)
    matches = [(i, o) for i, o in _failures(events) if signature(o) == target]
    if not matches:
        return None
    return {
        "count": len(matches),
        "last_seen": matches[-1][1]["timestamp"],
        "sessions": sorted({o["session"] for _, o in matches if o["session"]}),
        "last_fix": _last_fix(events, [i for i, _ in matches]),
    }


def stats(events: List[Dict], file: Optional[str] = None) -> List[Dict[str, Any]]:
    """Runs, success rate, durations and cost per file, last run first."""
    rows: Dict[str, Dict[str, Any]] = {}
    want = os.path.basename(file.replace("\\", "/")) if file else None
    for event in events:
        outcome = execution_outcome(event)
        if outcome is None:
            continue
        key = _run_key(outcome) or "<unknown>"
        if want and key != want:
            continue
        r = rows.setdefault(key, {"file": key, "runs": 0, "failures": 0, "durations": [],
                                  "costs": [], "errors": {}, "last_run": None})
        r["runs"] += 1
        r["last_run"] = outcome["timestamp"]
        if outcome["duration_s"] is not None:
            r["durations"].append(outcome["duration_s"])
        if outcome["cost_cu"] is not None:
            r["costs"].append(outcome["cost_cu"])
        if outcome["status"] == "error":
            r["failures"] += 1
            code = outcome["diagnosis"] or "unknown"
            r["errors"][code] = r["errors"].get(code, 0) + 1
    result = []
    for r in sorted(rows.values(), key=lambda r: r["last_run"] or "", reverse=True):
        durations, costs = r.pop("durations"), r.pop("costs")
        r["success_rate"] = round((r["runs"] - r["failures"]) / r["runs"], 3)
        r["median_duration_s"] = statistics.median(durations) if durations else None
        r["max_duration_s"] = max(durations) if durations else None
        r["median_cost_cu"] = round(statistics.median(costs), 4) if costs else None
        r["top_errors"] = dict(sorted(r.pop("errors").items(), key=lambda kv: -kv[1]))
        result.append(r)
    return result
