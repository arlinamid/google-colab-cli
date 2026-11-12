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

"""`colab exec --json`: stdout carries only JSON records, then a summary."""

import json
from unittest.mock import MagicMock

import nbformat
import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.runtime import SessionConnectionLost

runner = CliRunner()

CHDIR = "import os; os.makedirs('/content'"


def _tb(cell_line: int, code: str):
    return [
        "---------------------------------------------------------------------------",
        "ZeroDivisionError                         Traceback (most recent call last)",
        f"/tmp/ipykernel_9/123.py in <cell line: 0>()\n----> {cell_line} {code}\n",
        "ZeroDivisionError: division by zero",
    ]


def _error_for(code: str):
    """Kernel outputs for code that fails on its `1/0` line."""
    lines = code.splitlines()
    n = next(i for i, line in enumerate(lines, 1) if "1/0" in line)
    return [
        {"output_type": "stream", "name": "stdout", "text": "before\n"},
        {
            "output_type": "error",
            "ename": "ZeroDivisionError",
            "evalue": "division by zero",
            "traceback": _tb(n, lines[n - 1]),
        },
    ]


@pytest.fixture
def session(mock_common_state):
    s = MagicMock(url="http://url", token="tok", kernel_id=None, session_id=None)
    s.name = "s1"
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    return s


@pytest.fixture
def runtime(mocker, session):
    rt = mocker.patch("colab_cli.commands.execution.ColabRuntime").return_value

    def execute(code, output_hook=None, timeout=None):
        if code.startswith(CHDIR):
            return []
        if "1/0" in code:
            return _error_for(code)
        outputs = [{"output_type": "stream", "name": "stdout", "text": "hi\n"}]
        if output_hook:
            for o in outputs:
                output_hook(o)
        return outputs

    rt.execute_code.side_effect = execute
    return rt


def _records(result):
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]  # every stdout line is JSON


def test_stdin_error_record_and_summary(runtime):
    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="x = 1\n1/0\n")

    assert result.exit_code == 1
    cell, summary = _records(result)
    assert cell["schema"] == "colab.v1"
    assert cell["status"] == "error"
    assert cell["stdout"] == "before\n"
    assert cell["error"]["location"] == {
        "file": "<stdin>", "cell": None, "line": 2, "code": "1/0",
    }
    assert cell["diagnosis"]["code"] == "unknown"
    assert summary["type"] == "summary"
    assert summary["exit_code"] == 1
    assert summary["cells_failed"] == 1
    assert summary["session"] == "s1"


def test_file_line_numbers_skip_the_env_prelude(runtime, tmp_path):
    script = tmp_path / "train.py"
    script.write_text("import math\n1/0\n")

    result = runner.invoke(
        app, ["exec", "-s", "s1", "-f", str(script), "--env", "A=1", "--json"]
    )

    loc = _records(result)[0]["error"]["location"]
    assert loc["file"] == str(script)
    assert loc["line"] == 2


def test_success_keeps_stdout_clean(runtime):
    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="print('hi')\n")

    assert result.exit_code == 0
    cell, summary = _records(result)
    assert cell["status"] == "ok" and cell["stdout"] == "hi\n"
    assert summary["status"] == "ok"


def test_notebook_one_record_per_cell(runtime, tmp_path):
    nb = nbformat.v4.new_notebook()
    nb.cells = [
        nbformat.v4.new_code_cell("print('a')"),
        nbformat.v4.new_markdown_cell("text"),
        nbformat.v4.new_code_cell("1/0"),
    ]
    path = tmp_path / "nb.ipynb"
    nbformat.write(nb, str(path))

    result = runner.invoke(app, ["exec", "-s", "s1", "-f", str(path), "--json"])

    assert result.exit_code == 1
    first, second, summary = _records(result)
    assert first["cell_index"] == 0 and first["status"] == "ok"
    assert second["cell_index"] == 1
    assert second["error"]["location"]["cell"] == 2
    assert second["error"]["location"]["file"] == str(path)
    assert summary["cells_run"] == 2
    # Progress lines go to stderr, never into the JSON stream.
    assert "Executing cell" in result.stderr


def test_json_stream_emits_output_events(runtime):
    result = runner.invoke(
        app, ["exec", "-s", "s1", "--json-stream"], input="print('hi')\n"
    )

    event, cell, summary = _records(result)
    assert event == {
        "schema": "colab.v1", "type": "output", "cell_index": 0,
        "stream": "stdout", "text": "hi\n",
    }
    assert cell["type"] == "cell" and summary["type"] == "summary"


def test_timeout_is_exit_4_with_a_diagnosis(runtime):
    runtime.execute_code.side_effect = lambda code, **kw: (
        [] if code.startswith(CHDIR) else (_ for _ in ()).throw(TimeoutError("No output for 5 seconds."))
    )

    result = runner.invoke(
        app, ["exec", "-s", "s1", "--json", "--timeout", "5"], input="while True: pass\n"
    )

    assert result.exit_code == 4
    cell, summary = _records(result)
    assert cell["diagnosis"]["code"] == "idle_timeout"
    assert summary["exit_code"] == 4


def test_lost_session_is_exit_3(runtime):
    def execute(code, **kw):
        if code.startswith(CHDIR):
            return []
        raise SessionConnectionLost("s1")

    runtime.execute_code.side_effect = execute

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="print(1)\n")

    assert result.exit_code == 3
    cell, summary = _records(result)
    assert cell["diagnosis"]["code"] == "session_lost"
    assert summary["exit_code"] == 3


def test_env_var_turns_json_on(runtime, monkeypatch):
    monkeypatch.setenv("COLAB_CLI_JSON", "1")

    result = runner.invoke(app, ["exec", "-s", "s1"], input="print('hi')\n")

    assert [r["type"] for r in _records(result)] == ["cell", "summary"]


def test_history_keeps_the_diagnosis_code(runtime, mock_common_state):
    runner.invoke(app, ["exec", "-s", "s1", "--json"], input="1/0\n")

    events = [
        c.args for c in mock_common_state.history.log_event.call_args_list
        if c.args[1] == "execution"
    ]
    assert events[-1][2]["diagnosis"] == "unknown"


def test_without_json_output_is_unchanged(runtime):
    result = runner.invoke(app, ["exec", "-s", "s1"], input="print('hi')\n")

    assert result.exit_code == 0
    assert result.stdout == "hi\n"
