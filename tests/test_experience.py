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

"""Learning from the local history (docs/08, phase E)."""

import datetime

import pytest

from colab_cli.experience import (
    error_groups,
    execution_outcome,
    find_fix,
    installs_in,
    load_events,
    parse_since,
    seen_before,
    signature,
    stats,
)
from colab_cli.history import HistoryLogger

T0 = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.timezone.utc)


def ts(minutes):
    return (T0 + datetime.timedelta(minutes=minutes)).isoformat()


def failed(minutes, session="s1", file="train.py", module="torchvision", code="import torchvision\n", **extra):
    e = {
        "timestamp": ts(minutes), "event_type": "execution", "session": session,
        "code": code, "status": "error", "file": file, "label": file,
        "error": {"ename": "ModuleNotFoundError", "evalue": f"No module named '{module}'",
                  "location": {"file": file, "cell": None, "line": 1, "code": code.strip()}},
        "diagnosis": "missing_module", "duration_s": 1.0,
    }
    e.update(extra)
    return e


def ok(minutes, session="s1", file="train.py", code="import torchvision\n", duration=30.0, cost=None):
    e = {"timestamp": ts(minutes), "event_type": "execution", "session": session, "code": code,
         "status": "ok", "file": file, "label": file, "error": None, "diagnosis": None,
         "duration_s": duration}
    if cost is not None:
        e["cost_cu"] = cost
    return e


def install(minutes, *packages, session="s1"):
    items = ", ".join(f"'{p}'" for p in packages)
    return {"timestamp": ts(minutes), "event_type": "automation", "session": session, "op": "install",
            "code": f"\nimport subprocess, sys\ndef install():\n    packages = [{items}]\n"}


def upload(minutes, remote, session="s1"):
    return {"timestamp": ts(minutes), "event_type": "file_operation", "session": session,
            "op": "upload", "local": "x", "remote": remote}


OLD_TB = [
    "\x1b[0;31m---------------------------------------------------------------------------\x1b[0m",
    "\x1b[0;31mOSError\x1b[0m                                   Traceback (most recent call last)",
    "/tmp/ipykernel_1/2.py in <cell line: 0>()\n----> 1 save()\n",
    "OSError: [Errno 28] No space left on device",
]


def old_event(minutes, session="old"):
    """An execution event as logged before phase E: code and outputs only."""
    return {"timestamp": ts(minutes), "event_type": "execution", "session": session,
            "code": "save()\n", "cell_index": None, "cell_id": None,
            "outputs": [{"output_type": "error", "ename": "OSError",
                         "evalue": "[Errno 28] No space left on device", "traceback": OLD_TB}]}


# ---- outcomes and signatures ---------------------------------------------


def test_outcome_of_a_new_event():
    o = execution_outcome(failed(0))
    assert o["status"] == "error"
    assert o["diagnosis"] == "missing_module"
    assert o["where"] == "train.py:1"


def test_old_events_are_rediagnosed_from_their_outputs():
    o = execution_outcome(old_event(0))
    assert o["status"] == "error"
    assert o["ename"] == "OSError"
    assert o["diagnosis"] == "disk_full"


def test_old_successful_event():
    e = {"timestamp": ts(0), "event_type": "execution", "session": "s", "code": "1",
         "outputs": [{"output_type": "stream", "name": "stdout", "text": "1"}]}
    assert execution_outcome(e)["status"] == "ok"


def test_run_exit_zero_is_ok():
    e = {"timestamp": ts(0), "event_type": "execution", "session": "s", "code": "x", "via": "run",
         "outputs": [{"output_type": "error", "ename": "SystemExit", "evalue": "0", "traceback": []}]}
    assert execution_outcome(e)["status"] == "ok"


def test_signature_ignores_line_numbers_and_ids():
    a = execution_outcome(failed(0, code="x=1\nimport torchvision\n"))
    b = execution_outcome(failed(5))
    assert signature(a) == signature(b)
    c = execution_outcome(failed(9, module="timm"))
    assert signature(c) != signature(a)


def test_installs_in():
    assert installs_in(install(0, "torchvision", "timm==1.0")) == ["torchvision", "timm==1.0"]
    code = "!pip install -q torchvision timm\n%pip install pillow\nimport x\n"
    assert installs_in({"event_type": "execution", "code": code}) == ["torchvision", "timm", "pillow"]
    assert installs_in(ok(0)) == []


# ---- fixes ---------------------------------------------------------------


def test_fix_after_an_install():
    events = [failed(0), install(1, "torchvision"), ok(2)]
    fix = find_fix(events, 0)
    assert fix == {"after_s": 120.0, "attempts": 0, "code_changed": False,
                   "steps": [{"install": ["torchvision"]}]}


def test_fix_with_a_code_change_and_attempts():
    events = [failed(0), failed(1), upload(2, "/content/data.csv"),
              ok(3, code="import torchvision as tv\n")]
    fix = find_fix(events, 0)
    assert fix["attempts"] == 1
    assert fix["code_changed"] is True
    assert fix["steps"] == [{"upload": "/content/data.csv"}]


def test_no_fix_from_another_session_or_file_or_too_late():
    assert find_fix([failed(0), ok(1, session="other")], 0) is None
    assert find_fix([failed(0), ok(1, file="eval.py")], 0) is None
    assert find_fix([failed(0), ok(60 * 25)], 0) is None


# ---- grouping, seen_before, stats ----------------------------------------


def test_error_groups_newest_first_with_last_fix():
    events = [failed(0), install(1, "torchvision"), ok(2),
              failed(10, session="s2"), old_event(20), old_event(21)]
    groups = error_groups(events)
    assert [g["code"] for g in groups] == ["disk_full", "missing_module"]
    disk, missing = groups
    assert disk["count"] == 2 and disk["sessions"] == ["old"] and disk["last_fix"] is None
    assert missing["count"] == 2
    assert missing["sessions"] == ["s1", "s2"]
    assert missing["where"] == "train.py:1"
    assert missing["first_seen"] == ts(0) and missing["last_seen"] == ts(10)
    assert missing["last_fix"]["steps"] == [{"install": ["torchvision"]}]


def test_error_groups_limit():
    events = [failed(i, module=f"m{i}") for i in range(5)]
    assert len(error_groups(events, limit=2)) == 2


def test_seen_before():
    events = [failed(0), install(1, "torchvision"), ok(2)]
    new = execution_outcome(failed(30, session="s9"))
    seen = seen_before(events, new)
    assert seen["count"] == 1
    assert seen["sessions"] == ["s1"]
    assert seen["last_fix"]["steps"] == [{"install": ["torchvision"]}]
    assert seen_before(events, execution_outcome(failed(30, module="timm"))) is None


def test_stats_per_file():
    events = [failed(0), ok(2, duration=40.0, cost=0.1), ok(10, duration=20.0, cost=0.05),
              ok(20, file="eval.py", duration=5.0)]
    rows = {r["file"]: r for r in stats(events)}
    t = rows["train.py"]
    assert t["runs"] == 3 and t["failures"] == 1
    assert t["success_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert t["median_duration_s"] == 20.0
    assert t["max_duration_s"] == 40.0
    assert t["median_cost_cu"] == 0.075
    assert t["top_errors"] == {"missing_module": 1}
    assert t["last_run"] == ts(10)
    assert [r["file"] for r in stats(events, file="eval.py")] == ["eval.py"]


# ---- loading -------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [("7d", 7 * 86400), ("24h", 86400), ("30m", 1800), ("2w", 14 * 86400)])
def test_parse_since(text, expected):
    assert parse_since(text).total_seconds() == expected


def test_parse_since_rejects_garbage():
    with pytest.raises(ValueError):
        parse_since("soon")


def test_load_events_reads_all_sessions_sorted(tmp_path):
    import time

    history = HistoryLogger(str(tmp_path))
    history.log_event("b", "execution", {"code": "2", "outputs": []})
    time.sleep(0.05)  # distinct time stamps on coarse clocks
    history.log_event("a", "execution", {"code": "1", "outputs": []})
    events = load_events(history)
    assert [e["session"] for e in events] == ["b", "a"]
    assert [e["session"] for e in load_events(history, sessions=["a"])] == ["a"]
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    assert load_events(history, since=future) == []
