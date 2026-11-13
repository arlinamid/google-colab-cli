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
    mocker.patch.object(tm, "scan_forwards", return_value={})
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
    first = tm.collect_snapshot(world.state, now=NOW - tm.NEW_RUNTIME_WATCH_SEC)
    first = tm.collect_snapshot(world.state, prev=first, now=NOW)  # both seen for a while
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


def test_a_runtime_behind_a_forward_is_a_service(world):
    snap = tm.collect_snapshot(world.state, now=NOW, forwards={
        "idle": [{"local_port": 8188, "remote": "localhost:8188", "listening": True}],
        "?": [{"local_port": 1, "remote": "x", "listening": True}],
    })
    idle = snap["sessions"][1]
    assert idle["forwards"][0]["local_port"] == 8188
    assert snap["sessions"][2]["forwards"] == []  # orphans have no name to match
    card = tm.card_model(idle, now=NOW)
    assert card["state"] == "service" and card["open_url"] == "http://127.0.0.1:8188"

    down = tm.card_model({**idle, "forwards": [{"local_port": 8188, "remote": "x", "listening": False}]}, now=NOW)
    assert down["state"] == "service" and down["open_url"] is None


# --- profiles started by the tray (feedback while the runtime is created) ---

def test_start_progress_follows_the_profile_run_log():
    assert tm.start_progress("") == {"phase": "launching", "session": None, "line": None}
    log = "[colab] Creating session 'train-1113-180000'...\n"
    assert tm.start_progress(log) == {"phase": "allocating", "session": "train-1113-180000", "line": None}
    # A refused allocation is shown as it is.
    refused = log + "[colab] Allocation refused (precondition failed).\n"
    assert tm.start_progress(refused)["line"] == "Allocation refused (precondition failed)."
    # After READY the script's own output is the line; progress bars redraw with \r.
    log += "[colab] Session READY.\n$ pip install -q x\nDownloading model\r 10%\r 55%\n"
    assert tm.start_progress(log) == {"phase": "setup", "session": "train-1113-180000", "line": "55%"}
    assert tm.start_progress(log + "[colab] Started job j-1 (pid 7) on 'train-1113-180000'.\n")["phase"] == "ready"
    assert tm.start_progress(log + "[colab] Script finished on 'train-1113-180000'.\n")["phase"] == "ready"
    waiting = log + "[colab] Script finished on 'x'.\n[colab] Waiting for http://127.0.0.1:8188 ...\n"
    assert tm.start_progress(waiting)["phase"] == "waiting_ready"
    assert tm.start_progress(waiting + "[colab] Ready: http://127.0.0.1:8188 (Ctrl+C closes the forward).\n")[
        "phase"] == "ready"
    reuse = tm.start_progress("[colab] Reusing runtime 'ui'.\n")
    assert reuse["phase"] == "setup" and reuse["session"] == "ui"
    assert len(tm.start_progress("Downloading " + "x" * 300 + "\n")["line"]) <= 100


def _start(profile="train", phase="allocating", session=None, line=None, mode="job", hint=None):
    return {"profile": profile, "session": hint, "hardware": "T4", "mode": mode, "started_at": NOW - 42,
            "progress": {"phase": phase, "session": session, "line": line}}


def _card_entry(name, **kw):
    e = {"name": name, "endpoint": f"ep-{name}", "hardware": "T4", "local": True, "profile": None,
         "stop_when_done": None, "running": None, "activity": None, "last_execution_at": None,
         "jobs_checked_at": NOW, "job": None, "forwards": []}
    e.update(kw)
    return e


def test_a_starting_profile_has_a_card_until_its_runtime_shows_up():
    other = tm.card_model(_card_entry("other"), NOW)

    cards = tm.merge_starts([other], [_start(session="train-1113-180000")], NOW)

    assert [c["name"] for c in cards] == ["train-1113-180000", "other"]
    start = cards[0]
    assert start["state"] == "starting" and start["pending"] and start["phase"] == "allocating"
    assert start["elapsed_s"] == 42 and start["hardware"] == "T4" and start["profile"] == "train"
    # Before the log names the runtime, the card has the profile's name.
    assert tm.merge_starts([], [_start(phase="launching")], NOW)[0]["name"] == "train"

    # The runtime is in the snapshot: its own card takes over.
    mine = tm.card_model(_card_entry("train-1113-180000"), NOW)
    assert tm.merge_starts([mine], [_start(phase="setup", session="train-1113-180000")], NOW) == [mine]
    # Finished starting: nothing more to show.
    assert tm.merge_starts([], [_start(phase="ready", session="x")], NOW) == []


def test_a_kernel_profile_shows_its_setup_on_the_runtime_card():
    ui = tm.card_model(_card_entry("ui"), NOW, services={"ui"})

    [card] = tm.merge_starts([ui], [_start("ui", "setup", "ui", "Telepítés kész.", mode="kernel", hint="ui")], NOW)

    assert card["state"] == "starting" and not card["pending"]
    assert card["setup_line"] == "Telepítés kész." and card["phase"] == "setup"
    # A reused runtime is found by the profile's session before the log names it.
    [card] = tm.merge_starts([tm.card_model(_card_entry("ui"), NOW)],
                             [_start("ui", "launching", None, mode="kernel", hint="ui")], NOW)
    assert card["name"] == "ui" and card["state"] == "starting"
    # A running job keeps its state.
    busy = tm.card_model(_card_entry("ui", job={"id": "j", "state": "running", "overall_percent": 10.0}), NOW)
    [card] = tm.merge_starts([busy], [_start("ui", "setup", "ui", "x", mode="kernel", hint="ui")], NOW)
    assert card["state"] == "running"


def test_a_new_runtime_is_watched_for_its_first_job(world):
    """A profile's runtime shows up before its job starts; the job must not wait for the recheck."""
    first = tm.collect_snapshot(world.state, now=NOW)
    assert first["sessions"][1]["job"] is None and first["sessions"][1]["first_seen_at"] == NOW
    world.calls.clear()
    late = FakeFiles(_job_files("late-20261113-100100-bbbb"), world.calls)  # started on 'idle' just now
    trains = FakeFiles(world.files, world.calls)
    tm.ContentsClient.side_effect = lambda s: late if s.name == "idle" else trains

    second = tm.collect_snapshot(world.state, prev=first, now=NOW + 30)

    assert second["sessions"][1]["job"]["id"] == "late-20261113-100100-bbbb"
    assert second["sessions"][1]["first_seen_at"] == NOW
    # Later, an idle runtime goes back to the slow recheck.
    idle_later = tm.collect_snapshot(world.state, prev=first, now=NOW + tm.NEW_RUNTIME_WATCH_SEC + 1)
    assert tm.NEW_RUNTIME_WATCH_SEC <= 180 < tm.IDLE_RECHECK_SEC
    assert idle_later["sessions"][1]["first_seen_at"] == NOW
