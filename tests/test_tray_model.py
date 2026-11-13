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

"""The tray's snapshot and the pure functions the panel draws from (docs/09)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from colab_cli import tray_model as tm
from colab_cli.jobs import JOBS_DIR
from colab_cli.state import SessionState

NOW = 1_000_000.0


def _assignment(endpoint, accelerator="T4"):
    return SimpleNamespace(endpoint=endpoint, accelerator=SimpleNamespace(value=accelerator),
                           variant=SimpleNamespace(name="GPU"), machine_shape=SimpleNamespace(name="STANDARD"))


class FakeFiles:
    def __init__(self, files, calls):
        self.files = files
        self.calls = calls

    def list_dir(self, path):
        self.calls.append(("list", path))
        names = {p[len(path) + 1:].split("/")[0] for p in self.files if p.startswith(path + "/")}
        if not names:
            raise FileNotFoundError(path)
        return {"content": [{"name": n, "type": "directory"} for n in sorted(names)]}

    def read_text(self, path):
        self.calls.append(("read", path))
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def _job_files(job_id, state="running", **status):
    base = f"{JOBS_DIR}/{job_id}"
    st = {"id": job_id, "name": "train", "argv": ["python"], "state": state, "pid": 1,
          "started_at": NOW - 600, "ended_at": None if state == "running" else NOW - 10,
          "exit_code": None if state == "running" else (0 if state == "succeeded" else 1),
          "last_output_at": NOW - 30,
          "progress": {"percent": 45, "n": 45, "total": 100, "eta_s": 720}, **status}
    return {
        f"{base}/status.json": json.dumps(st),
        f"{base}/metrics.jsonl": "\n".join(json.dumps(r) for r in [
            {"t": 1, "epoch": 2, "epochs": 5, "loss": 0.5},
            {"t": 2, "epoch": 2, "epochs": 5, "loss": 0.31},
        ]) + "\n",
        f"{base}/tail.txt": "Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n"
        if state == "failed" else "step\n",
    }


@pytest.fixture
def world(mocker):
    """Two runtimes: 'train' (T4, a job) and 'idle' (CPU); one orphan."""
    calls = []
    files = {**_job_files("train-20261113-100000-aaaa")}
    train = SessionState(name="train", token="t", url="u", endpoint="ep1", accelerator="T4",
                         profile="train-t4", stop_when_done=True)
    idle = SessionState(name="idle", token="t", url="u", endpoint="ep2",
                        last_execution=("x.py", None, "2026-11-13 08:00:00"))
    st = MagicMock()
    st.sync_sessions.return_value = (
        {"train": train, "idle": idle},
        [_assignment("ep1"), _assignment("ep2", "NONE"), _assignment("ep9", "L4")],
    )
    st.client.get_consumption_user_info.return_value = SimpleNamespace(
        paid_compute_units_balance=87.0, consumption_rate_hourly=1.9, assignments_count=3)
    by_session = {"train": FakeFiles(files, calls), "idle": FakeFiles({}, calls)}
    mocker.patch.object(tm, "ContentsClient", side_effect=lambda s: by_session[s.name])
    return SimpleNamespace(state=st, calls=calls, files=files, train=train, idle=idle)


def test_snapshot_collects_runtimes_jobs_and_account(world):
    snap = tm.collect_snapshot(world.state, now=NOW)

    assert snap["ok"] is True and snap["error"] is None
    assert snap["account"]["balance_cu"] == 87.0 and snap["account"]["rate_cu_per_hour"] == 1.9
    names = [s["name"] for s in snap["sessions"]]
    assert names == ["train", "idle", "?"]
    train = snap["sessions"][0]
    assert train["hardware"] == "T4" and train["profile"] == "train-t4"
    assert train["job"]["id"] == "train-20261113-100000-aaaa"
    assert train["job"]["overall_percent"] == 29.0
    assert snap["sessions"][1]["job"] is None
    assert snap["sessions"][2]["local"] is False  # an orphan: no proxy access


def test_idle_runtimes_are_not_contacted_on_every_poll(world):
    first = tm.collect_snapshot(world.state, now=NOW)
    world.calls.clear()

    tm.collect_snapshot(world.state, prev=first, now=NOW + 30)

    touched = {path for _, path in world.calls}
    assert any("train-20261113" in p for p in touched)  # the running job is followed
    assert all(p == JOBS_DIR or "train" in p for p in touched)
    assert ("list", JOBS_DIR) in world.calls  # train's dir, for a newer job
    # 'idle' had no job: not listed again until the recheck interval.
    assert world.calls.count(("list", JOBS_DIR)) == 1

    world.calls.clear()
    tm.collect_snapshot(world.state, prev=first, now=NOW + tm.IDLE_RECHECK_SEC + 1)
    assert world.calls.count(("list", JOBS_DIR)) == 2


def test_account_is_fetched_every_few_minutes(world):
    first = tm.collect_snapshot(world.state, now=NOW)
    tm.collect_snapshot(world.state, prev=first, now=NOW + 60)
    assert world.state.client.get_consumption_user_info.call_count == 1
    tm.collect_snapshot(world.state, prev=first, now=NOW + tm.ACCOUNT_EVERY_SEC + 1)
    assert world.state.client.get_consumption_user_info.call_count == 2


def test_a_failed_refresh_keeps_the_last_data_and_says_so(world):
    first = tm.collect_snapshot(world.state, now=NOW)
    world.state.sync_sessions.side_effect = OSError("network down")

    snap = tm.collect_snapshot(world.state, prev=first, now=NOW + 30)

    assert snap["ok"] is False and "network down" in snap["error"]
    assert snap["sessions"] == first["sessions"]
    assert snap["last_ok_at"] == first["taken_at"]


# --- the pure functions ---

def _snap(world, now=NOW, prev=None):
    return tm.collect_snapshot(world.state, prev=prev, now=now)


def test_icon_state_levels_with_text(world):
    snap = _snap(world)
    state = tm.icon_state(snap, now=NOW, interval=30)
    assert state["level"] == "running"
    assert "train" in state["text"] and "29%" in state["text"]

    empty = {**snap, "sessions": []}
    assert tm.icon_state(empty, now=NOW, interval=30)["level"] == "none"

    failed = {**snap, "ok": False, "error": "boom"}
    s = tm.icon_state(failed, now=NOW, interval=30)
    assert s["level"] == "error" and "boom" in s["text"]

    stale = tm.icon_state(snap, now=NOW + 61, interval=30)
    assert stale["level"] == "stale" and "1 min" in stale["text"]


def test_icon_state_failed_job_and_low_balance(world):
    world.files.update(_job_files("train-20261113-100000-aaaa", state="failed"))
    snap = _snap(world)
    assert tm.icon_state(snap, now=NOW, interval=30)["level"] == "error"

    world.files.update(_job_files("train-20261113-100000-aaaa"))
    world.state.client.get_consumption_user_info.return_value = SimpleNamespace(
        paid_compute_units_balance=1.0, consumption_rate_hourly=1.9, assignments_count=1)
    s = tm.icon_state(_snap(world), now=NOW, interval=30)
    assert s["level"] == "warning" and "balance" in s["text"]


def test_card_with_progress(world):
    card = tm.card_model(_snap(world)["sessions"][0], now=NOW)
    assert card["state"] == "running"
    assert card["percent"] == 29.0
    assert card["metric"] == {"name": "loss", "value": 0.31, "trend": "down"}
    assert card["epoch"] == (2, 5) and card["eta_s"] == 720
    assert card["can_stop_job"] is True


def test_card_without_progress_shows_the_last_output(world):
    world.files.update(_job_files("train-20261113-100000-aaaa", progress=None))
    base = f"{JOBS_DIR}/train-20261113-100000-aaaa"
    world.files[f"{base}/metrics.jsonl"] = ""
    card = tm.card_model(_snap(world)["sessions"][0], now=NOW)
    assert card["percent"] is None
    assert card["last_output_age_s"] == 30.0


def test_card_for_a_foreground_exec_and_an_idle_runtime(world):
    world.idle.running = "exec(nb.ipynb)"
    world.idle.activity = {"file": "nb.ipynb", "cell": 3, "total": 12, "label": "cell 3 (Setup)",
                           "started_at": "2026-11-13T09:00:00"}
    card = tm.card_model(_snap(world)["sessions"][1], now=NOW)
    assert card["state"] == "running" and card["cell"] == (3, 12, "cell 3 (Setup)")
    assert card["percent"] == pytest.approx(100 * 2 / 12, abs=0.1)
    assert card["can_stop_job"] is False

    world.idle.running = None
    world.idle.activity = None
    card = tm.card_model(_snap(world)["sessions"][1], now=NOW)
    assert card["state"] == "idle" and card["percent"] is None


def test_cu_model_is_an_estimate():
    cu = tm.cu_model({"balance_cu": 87.0, "rate_cu_per_hour": 1.9})
    assert cu["hours_left"] == pytest.approx(45.8, abs=0.1) and cu["estimate"] is True
    assert tm.cu_model({"balance_cu": 87.0, "rate_cu_per_hour": 0})["hours_left"] is None
    assert tm.cu_model(None) is None


def test_events_between_snapshots(world):
    first = _snap(world)
    world.files.update(_job_files("train-20261113-100000-aaaa", state="succeeded"))
    second = _snap(world, now=NOW + 30, prev=first)
    kinds = [e["kind"] for e in tm.events(first, second)]
    assert kinds == ["job_done"]
    assert tm.events(second, second) == []

    world.state.sync_sessions.return_value = (
        {"idle": world.idle}, [_assignment("ep2", "NONE")])
    third = _snap(world, now=NOW + 60, prev=second)
    assert [e["kind"] for e in tm.events(second, third)] == ["session_gone"]


def test_failed_job_event_carries_the_diagnosis(world):
    first = _snap(world)
    world.files.update(_job_files("train-20261113-100000-aaaa", state="failed"))
    second = _snap(world, now=NOW + 30, prev=first)
    (event,) = tm.events(first, second)
    assert event["kind"] == "job_failed" and event["code"] == "missing_module"


def test_stop_when_done_targets(world):
    first = _snap(world)
    assert tm.sessions_to_stop(first, set()) == []
    world.files.update(_job_files("train-20261113-100000-aaaa", state="succeeded"))
    second = _snap(world, now=NOW + 30, prev=first)
    assert tm.sessions_to_stop(second, set()) == ["train"]
    assert tm.sessions_to_stop(second, {"train"}) == []  # already asked to stop


def test_a_session_created_after_the_tray_started_is_not_an_orphan(world, mocker):
    """The CLI state caches the local sessions for one command; the tray
    lives for hours, so each snapshot must read them again."""
    from colab_cli.common import State

    st = State()
    st._client = world.state.client
    st._store = MagicMock()
    st._history = MagicMock()
    st._store.list.return_value = {}
    world.state.client.list_assignments.return_value = []
    first = tm.collect_snapshot(st, now=NOW)
    assert first["sessions"] == []

    late = SessionState(name="late", token="t", url="u", endpoint="ep7")
    st._store.list.return_value = {"late": late}
    a = _assignment("ep7")
    a.runtime_proxy_info = SimpleNamespace(token="t2", url="u2", expires_at=lambda: None)
    world.state.client.list_assignments.return_value = [a]
    mocker.patch.object(tm, "ContentsClient", return_value=FakeFiles({}, []))
    second = tm.collect_snapshot(st, prev=first, now=NOW + 30)

    assert second["ok"], second["error"]
    assert [s["name"] for s in second["sessions"]] == ["late"]
    assert second["sessions"][0]["local"] is True
