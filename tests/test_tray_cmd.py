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

"""`colab tray`: the snapshot, the checks before the GUI starts, --detach (docs/09)."""

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import tray as tray_cmd

runner = CliRunner()

SNAP = {
    "schema": "colab.v1", "type": "tray_snapshot", "taken_at": 100.0, "last_ok_at": 100.0,
    "ok": True, "error": None,
    "account": {"balance_cu": 87.0, "rate_cu_per_hour": 1.9, "assignments": 1, "fetched_at": 100.0},
    "sessions": [{
        "name": "train", "endpoint": "e", "hardware": "T4", "local": True, "profile": None,
        "stop_when_done": None, "running": None, "activity": None, "last_execution_at": None,
        "jobs_checked_at": 100.0,
        "job": {"id": "j", "state": "running", "overall_percent": 45.0, "progress": {"eta_s": 60},
                "epoch": None, "cell": None, "last_output_age_s": 5.0, "stalled": False,
                "metrics": {"trend": {}}, "diagnosis": None, "stop_reason": None},
    }],
}


@pytest.fixture
def snap(mocker, mock_common_state):
    mocker.patch.object(tray_cmd, "collect_snapshot", return_value=SNAP)
    mocker.patch.object(tray_cmd.time, "time", return_value=100.0)


def test_snapshot_json(snap):
    result = runner.invoke(app, ["tray", "--snapshot", "--json"])

    assert result.exit_code == 0, result.output
    rec = json.loads(result.stdout)
    assert rec["type"] == "tray_snapshot"
    assert rec["icon"]["level"] == "running"
    assert rec["cards"][0]["percent"] == 45.0
    assert rec["cu"]["hours_left"] == pytest.approx(45.8, abs=0.1) and rec["cu"]["estimate"] is True


def test_snapshot_human(snap):
    result = runner.invoke(app, ["tray", "--snapshot"])

    assert result.exit_code == 0, result.output
    assert "train" in result.output and "45%" in result.output
    assert "estimated" in result.output


def test_missing_pyside6_gives_the_install_hint(mocker, mock_common_state):
    mocker.patch.object(tray_cmd, "_gui_problem", return_value=None)
    mocker.patch.object(tray_cmd, "_load_gui", side_effect=ImportError("No module named 'PySide6'"))

    result = runner.invoke(app, ["tray"])

    assert result.exit_code == 2
    assert "google-colab-cli[tray]" in result.output


@pytest.mark.parametrize("system, env, release, needle", [
    ("linux", {}, "6.1.0", "display"),
    ("linux", {"DISPLAY": ":0"}, "5.15.0-microsoft-standard-WSL2", "WSL"),
    ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, "6.1.0", None),
    ("win32", {}, "10", None),
    ("darwin", {}, "23.0", None),
])
def test_gui_problem(system, env, release, needle):
    problem = tray_cmd._gui_problem(system=system, env=env, release=release)
    assert (problem is None) if needle is None else (needle in problem)


def test_no_desktop_is_refused(mocker, mock_common_state):
    mocker.patch.object(tray_cmd, "_gui_problem", return_value="no display (x).")
    load = mocker.patch.object(tray_cmd, "_load_gui")

    result = runner.invoke(app, ["tray"])

    assert result.exit_code == 2
    assert "no display" in result.output and "--snapshot" in result.output
    load.assert_not_called()


def test_a_second_tray_is_refused(mocker, mock_common_state, tmp_path):
    import filelock

    mock_common_state.config_path = str(tmp_path / "sessions.json")
    mocker.patch.object(tray_cmd, "_gui_problem", return_value=None)
    gui = MagicMock()
    mocker.patch.object(tray_cmd, "_load_gui", return_value=gui)
    held = filelock.FileLock(str(tmp_path / "tray.lock"))
    held.acquire()
    try:
        result = runner.invoke(app, ["tray"])
    finally:
        held.release()

    assert result.exit_code == 1
    assert "already running" in result.output
    gui.assert_not_called()


def test_tray_runs_the_gui_with_the_lock(mocker, mock_common_state, tmp_path):
    mock_common_state.config_path = str(tmp_path / "sessions.json")
    mocker.patch.object(tray_cmd, "_gui_problem", return_value=None)
    gui = MagicMock(return_value=0)
    mocker.patch.object(tray_cmd, "_load_gui", return_value=gui)

    result = runner.invoke(app, ["tray", "--interval", "15"])

    assert result.exit_code == 0, result.output
    assert gui.call_args.kwargs["interval"] == 15


def test_detach_starts_itself_in_the_background(mocker, mock_common_state):
    mocker.patch.object(tray_cmd, "_gui_problem", return_value=None)
    mocker.patch.object(tray_cmd, "_load_gui", return_value=MagicMock())
    mocker.patch.object(tray_cmd, "colab_argv", return_value=["colab", "--auth=oauth2"])
    popen = mocker.patch.object(tray_cmd.subprocess, "Popen")

    result = runner.invoke(app, ["tray", "--detach", "--interval", "20"])

    assert result.exit_code == 0, result.output
    argv = popen.call_args.args[0]
    assert argv == ["colab", "--auth=oauth2", "tray", "--interval", "20"]
    kw = popen.call_args.kwargs
    assert kw["stdin"] is tray_cmd.subprocess.DEVNULL
    if tray_cmd.sys.platform == "win32":
        assert kw["creationflags"]
    else:
        assert kw["start_new_session"] is True
