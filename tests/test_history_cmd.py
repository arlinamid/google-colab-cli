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

"""Phase E wiring: richer execution events, seen_before, `colab history`."""

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from test_experience import failed, install, ok, old_event

runner = CliRunner()
CHDIR = "import os; os.makedirs('/content'"


def _tb(line, code):
    return [f"/tmp/ipykernel_1/2.py in <cell line: 0>()\n----> {line} {code}\n",
            "ModuleNotFoundError: No module named 'torchvision'"]


MISSING = [{"output_type": "error", "ename": "ModuleNotFoundError",
            "evalue": "No module named 'torchvision'", "traceback": _tb(1, "import torchvision")}]


@pytest.fixture
def session(mock_common_state, mocker):
    s = MagicMock(url="http://u", token="t", kernel_id=None, session_id=None)
    s.name = "s1"
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    rt = mocker.patch("colab_cli.commands.execution.ColabRuntime").return_value
    rt.execute_code.side_effect = lambda code, **kw: [] if code.startswith(CHDIR) else MISSING
    return mock_common_state


def _with_history(state, events):
    by_session = {}
    for e in events:
        by_session.setdefault(e["session"], []).append({k: v for k, v in e.items() if k != "session"})
    state.history.list_sessions.return_value = list(by_session)
    state.history.get_history.side_effect = lambda name: by_session.get(name, [])


def _execution_events(state):
    return [c.args[2] for c in state.history.log_event.call_args_list if c.args[1] == "execution"]


def test_exec_logs_status_error_and_diagnosis(session, tmp_path):
    script = tmp_path / "train.py"
    script.write_text("import torchvision\n")

    runner.invoke(app, ["exec", "-s", "s1", "-f", str(script)])

    event = _execution_events(session)[-1]
    assert event["status"] == "error"
    assert event["file"] == str(script)
    assert event["label"] == str(script)
    assert event["diagnosis"] == "missing_module"
    assert event["error"]["ename"] == "ModuleNotFoundError"
    assert event["error"]["location"]["line"] == 1
    assert event["duration_s"] >= 0


def test_exec_json_reports_seen_before_with_the_last_fix(session):
    _with_history(session, [failed(0, session="old"), install(1, "torchvision", session="old"),
                            ok(2, session="old")])

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="import torchvision\n")

    cell = json.loads(result.stdout.splitlines()[0])
    seen = cell["diagnosis"]["seen_before"]
    assert seen["count"] == 1
    assert seen["sessions"] == ["old"]
    assert seen["last_fix"]["steps"] == [{"install": ["torchvision"]}]


def test_exec_json_new_error_has_null_seen_before(session):
    _with_history(session, [])

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="import torchvision\n")

    assert json.loads(result.stdout.splitlines()[0])["diagnosis"]["seen_before"] is None


def test_a_broken_history_never_breaks_exec(session):
    session.history.list_sessions.side_effect = OSError("disk")

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="import torchvision\n")

    assert result.exit_code == 1
    assert json.loads(result.stdout.splitlines()[0])["diagnosis"]["seen_before"] is None


def test_history_errors_json(mock_common_state):
    _with_history(mock_common_state, [failed(0), install(1, "torchvision"), ok(2), old_event(5)])

    result = runner.invoke(app, ["history", "errors", "--json"])

    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["type"] for r in rows] == ["error_group", "error_group"]
    assert rows[0]["code"] == "disk_full"
    assert rows[1]["last_fix"]["steps"] == [{"install": ["torchvision"]}]
    assert all(r["schema"] == "colab.v1" for r in rows)


def test_history_errors_human(mock_common_state):
    _with_history(mock_common_state, [failed(0), install(1, "torchvision"), ok(2)])

    result = runner.invoke(app, ["history", "errors"])

    assert "missing_module" in result.output
    assert "install torchvision" in result.output


def test_history_stats_for_a_file(mock_common_state):
    _with_history(mock_common_state, [failed(0), ok(2, duration=40.0), ok(9, file="eval.py")])

    result = runner.invoke(app, ["history", "stats", "train.py", "--json"])

    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == 1
    assert rows[0]["type"] == "file_stats" and rows[0]["file"] == "train.py"
    assert rows[0]["runs"] == 2


def test_history_session_filter_and_since(mock_common_state):
    _with_history(mock_common_state, [failed(0, session="a"), failed(1, session="b", module="timm")])

    result = runner.invoke(app, ["history", "errors", "-s", "b", "--json"])
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["sessions"] for r in rows] == [["b"]]

    # The synthetic events are from 2026-11-10; a 1 minute window has none.
    result = runner.invoke(app, ["history", "errors", "--since", "1m"])
    assert "No failures" in result.output


def test_history_bad_since(mock_common_state):
    result = runner.invoke(app, ["history", "errors", "--since", "soon"])
    assert result.exit_code == 2
