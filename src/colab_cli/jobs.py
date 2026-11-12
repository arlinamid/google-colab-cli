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

"""Detached jobs on the runtime (`colab job`, docs/08 phase B): CLI side.

`colab job start` sends build_start_code() to the kernel once: it writes the
job folder (see job_vm.py) and starts the runner in its own session, then the
CLI exits. Everything later reads the job's files over the Contents API, so
no kernel round trip is needed and a busy kernel does not matter.
"""

import base64
import datetime
import json
import re
import secrets
from typing import Any, Dict, List, Optional

from colab_cli.diagnosis import _result, diagnose, diagnose_failure
from colab_cli.job_vm import FEEDBACK_SOURCE, RUNNER_SOURCE
from colab_cli.results import SCHEMA

JOBS_DIR = "/content/.colab-jobs"
RUNNING_STATES = ("starting", "running", "stopping")
DEFAULT_STALL_AFTER = 600.0

_TRACEBACK = "Traceback (most recent call last):"
_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")


def new_job_id(name: Optional[str]) -> str:
    """``<name>-YYYYMMDD-HHMMSS-xxxx``: sorts by start time, safe as a path."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "job").lower()).strip("-") or "job"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug[:40]}-{stamp}-{secrets.token_hex(2)}"


def job_argv(file: Optional[str], cmd: Optional[List[str]], args: List[str]) -> List[str]:
    """The job's argv. ``{python}`` and ``{dir}`` are filled in on the runtime.

    A script runs with the kernel's Python; one command string runs in bash;
    several words are an exact argv (like `colab ssh -- CMD`).
    """
    if file:
        return ["{python}", "-u", "{dir}/script.py", *args]
    if len(cmd) == 1:
        return ["bash", "-c", cmd[0]]
    return list(cmd)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def build_start_code(
    job_id: str,
    *,
    name: Optional[str],
    argv: List[str],
    env: Dict[str, str],
    script_source: Optional[str] = None,
    jobs_dir: str = JOBS_DIR,
    max_seconds: Optional[float] = None,
) -> str:
    """Kernel code that creates the job folder and starts the runner.

    The spec file on disk holds argv only; ``env`` (which may carry secrets
    from --secret) goes straight into the process environment.
    """
    folder = f"{jobs_dir}/{job_id}"
    files = {"runner.py": _b64(RUNNER_SOURCE), "lib/colab_feedback.py": _b64(FEEDBACK_SOURCE)}
    if script_source is not None:
        files["script.py"] = _b64(script_source)
    spec = {"id": job_id, "name": name, "argv": argv, "cwd": "/content"}
    if max_seconds:
        spec["max_seconds"] = round(max_seconds, 1)
    return (
        "def _colab_job_start():\n"
        "    import base64, json, os, subprocess, sys\n"
        f"    _dir = {folder!r}\n"
        "    os.makedirs(os.path.join(_dir, 'lib'), exist_ok=True)\n"
        f"    for _name, _data in {files!r}.items():\n"
        "        with open(os.path.join(_dir, _name), 'wb') as _f:\n"
        "            _f.write(base64.b64decode(_data))\n"
        f"    _spec = {spec!r}\n"
        "    _spec['argv'] = [a.replace('{python}', sys.executable).replace('{dir}', _dir) for a in _spec['argv']]\n"
        "    if not os.path.isdir(_spec['cwd']):\n"
        "        _spec['cwd'] = _dir\n"
        "    with open(os.path.join(_dir, 'spec.json'), 'w', encoding='utf-8') as _f:\n"
        "        json.dump(_spec, _f)\n"
        "    _env = dict(os.environ)\n"
        f"    _env.update({env!r})\n"
        "    _env['COLAB_JOB_DIR'] = _dir\n"
        "    _env['PYTHONPATH'] = os.pathsep.join(p for p in [os.path.join(_dir, 'lib'), _env.get('PYTHONPATH', '')] if p)\n"
        "    _err = open(os.path.join(_dir, 'runner.err'), 'ab')\n"
        "    _p = subprocess.Popen(\n"
        "        [sys.executable, '-u', os.path.join(_dir, 'runner.py'), _dir],\n"
        "        env=_env, cwd=_spec['cwd'], stdin=subprocess.DEVNULL, stdout=_err,\n"
        "        stderr=_err, start_new_session=True, close_fds=True,\n"
        "    )\n"
        f"    print('COLAB_JOB ' + json.dumps({{'id': {job_id!r}, 'pid': _p.pid, 'dir': _dir}}))\n"
        "_colab_job_start()\n"
        "del _colab_job_start\n"
    )


def metric_trend(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """first/last/min/max of every numeric metric (not t or step)."""
    trend: Dict[str, Dict[str, float]] = {}
    for row in rows:
        for key, value in row.items():
            if key in ("t", "step") or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            t = trend.setdefault(key, {"first": value, "last": value, "min": value, "max": value})
            t["last"] = value
            t["min"] = min(t["min"], value)
            t["max"] = max(t["max"], value)
    return trend


def diagnose_log(tail: str, *, session: Optional[str], file: Optional[str] = None) -> Dict:
    """Diagnose a failed job from the Python traceback at the end of its log."""
    lines = tail.splitlines()
    start = max((i for i, line in enumerate(lines) if line.startswith(_TRACEBACK)), default=None)
    if start is None:
        return diagnose("", "The job failed; see its output.", tail, session=session, file=file)
    for line in reversed(lines[start + 1:]):
        m = _EXCEPTION_LINE.match(line.strip())
        if m and not line.startswith(" "):
            ename = m.group(1).rsplit(".", 1)[-1]
            return diagnose(ename, m.group(2) or "", "\n".join(lines[start:]), session=session, file=file)
    return diagnose("", "The job failed; see its output.", tail, session=session, file=file)


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")


def _stalled(job_id: str, session: Optional[str], silent: float) -> Dict:
    actions = []
    if session:
        actions = [
            {"cmd": f"colab job tail -s {session} {job_id}", "why": "see the last output"},
            {"cmd": f"colab inspect -s {session} --gpu", "why": "check whether it still uses the GPU"},
            {"cmd": f"colab job stop -s {session} {job_id}", "why": "stop it if it hangs"},
        ]
    return _result("stalled", f"No output for {silent:.0f} s; the job may hang.", actions)


def job_record(
    status: Dict[str, Any],
    *,
    metrics: List[Dict[str, Any]],
    tail: Optional[str],
    now: float,
    stall_after: float,
    session: Optional[str],
    file: Optional[str] = None,
    tail_lines: int = 20,
    cost: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The `colab job status --json` record for one job."""
    state = status.get("state")
    running = state in RUNNING_STATES
    started = status.get("started_at")
    ended = status.get("ended_at")
    last_output = status.get("last_output_at") or started
    silent = (now - last_output) if running and last_output else None
    stalled = bool(running and silent is not None and silent > stall_after)
    diagnosis = None
    if state == "failed" and tail is not None:  # None: the output was not read
        diagnosis = diagnose_log(tail, session=session, file=file)
    elif state == "stopped" and status.get("stop_reason") == "budget":
        diagnosis = diagnose_failure("budget_reached", session=session)
    elif stalled:
        diagnosis = _stalled(status.get("id"), session, silent)
    last = {k: v for k, v in (metrics[-1] if metrics else {}).items() if k != "t"}
    return {
        "schema": SCHEMA,
        "type": "job",
        "id": status.get("id"),
        "name": status.get("name"),
        "session": session,
        "state": state,
        "stop_reason": status.get("stop_reason"),
        "exit_code": status.get("exit_code"),
        "pid": status.get("pid"),
        "argv": status.get("argv"),
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "elapsed_s": round(((ended or now) - started), 1) if started else None,
        "silent_s": round(silent, 1) if silent is not None else None,
        "stalled": stalled,
        "progress": status.get("progress"),
        "metrics": {"last": last or None, "trend": metric_trend(metrics), "count": len(metrics)},
        "tail": tail.splitlines()[-tail_lines:] if tail else [],
        "diagnosis": diagnosis,
        "cost": cost,
    }


def new_lines(prev: List[str], cur: List[str]) -> List[str]:
    """Lines of ``cur`` after the longest end of ``prev`` it starts with."""
    for k in range(min(len(prev), len(cur)), 0, -1):
        if prev[-k:] == cur[:k]:
            return cur[k:]
    return cur


def parse_metrics(text: str) -> List[Dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
