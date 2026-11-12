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

"""`colab run --json`: one record for the script, then a summary.

The process exit code stays the script's own (as with `python script.py`);
a timeout is 4 and a lost session 3.
"""

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.client import PostAssignmentResponse, RuntimeProxyInfo

runner = CliRunner()
CHDIR = "import os; os.makedirs('/content'"


@pytest.fixture
def runtime(mocker, mock_common_state):
    res = MagicMock()
    res.__class__ = PostAssignmentResponse
    res.runtime_proxy_info = RuntimeProxyInfo(
        token="tok", tokenExpiresInSeconds=3600, url="http://runtime"
    )
    res.endpoint = "ep-1"
    mock_common_state.client.assign.return_value = res
    persisted = {}
    mock_common_state.store.add.side_effect = lambda s: persisted.__setitem__("s", s)
    mock_common_state.store.get.side_effect = lambda name: persisted.get("s")
    return mocker.patch("colab_cli.commands.run.ColabRuntime").return_value


def _fail_on(marker, ename="ZeroDivisionError", evalue="division by zero"):
    def execute(code, output_hook=None, timeout=None):
        if code.startswith(CHDIR):
            return []
        lines = code.splitlines()
        n = next(i for i, line in enumerate(lines, 1) if marker in line)
        return [
            {"output_type": "stream", "name": "stdout", "text": "start\n"},
            {
                "output_type": "error",
                "ename": ename,
                "evalue": evalue,
                "traceback": [
                    f"/tmp/ipykernel_1/2.py in <cell line: 0>()\n----> {n} {lines[n - 1]}\n",
                    f"{ename}: {evalue}",
                ],
            },
        ]

    return execute


def _records(result):
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_error_maps_to_the_script_line_past_shebang_and_prelude(runtime, tmp_path):
    script = tmp_path / "job.py"
    script.write_text("#!/usr/bin/env -S colab run\nx = 1\n1/0\n")
    runtime.execute_code.side_effect = _fail_on("1/0")

    result = runner.invoke(app, ["run", "--json", "--env", "A=1", str(script)])

    assert result.exit_code == 1
    cell, summary = _records(result)
    assert cell["status"] == "error"
    assert cell["stdout"] == "start\n"
    assert cell["error"]["location"] == {
        "file": str(script), "cell": None, "line": 3, "code": "1/0",
    }
    # The runtime is already stopped: no `-s NAME` to suggest.
    assert cell["session"] is None
    assert summary["exit_code"] == 1
    assert summary["session"].startswith("run-")


def test_script_exit_code_is_kept(runtime, tmp_path):
    script = tmp_path / "exit.py"
    script.write_text("import sys\nsys.exit(3)\n")
    runtime.execute_code.side_effect = _fail_on("sys.exit", "SystemExit", "3")

    result = runner.invoke(app, ["run", "--json", str(script)])

    assert result.exit_code == 3
    cell, summary = _records(result)
    assert cell["exit_code"] == 3
    assert cell["diagnosis"]["code"] == "script_exit"
    assert summary["exit_code"] == 3


def test_success_output_goes_into_the_record(runtime, tmp_path, mock_common_state):
    script = tmp_path / "ok.py"
    script.write_text("print('hello')\n")

    def execute(code, output_hook=None, timeout=None):
        outputs = [] if code.startswith(CHDIR) else [
            {"output_type": "stream", "name": "stdout", "text": "hello\n"}
        ]
        for o in outputs:
            if output_hook:
                output_hook(o)
        return outputs

    runtime.execute_code.side_effect = execute

    result = runner.invoke(app, ["run", "--json", str(script)])

    assert result.exit_code == 0
    cell, summary = _records(result)
    assert cell["status"] == "ok" and cell["stdout"] == "hello\n"
    assert summary["status"] == "ok"
    # The runtime is still released.
    mock_common_state.client.unassign.assert_called_once()


def test_keep_points_next_actions_at_the_session(runtime, tmp_path):
    script = tmp_path / "m.py"
    script.write_text("import torchvision\n")
    runtime.execute_code.side_effect = _fail_on(
        "import torchvision", "ModuleNotFoundError", "No module named 'torchvision'"
    )

    result = runner.invoke(app, ["run", "--json", "--keep", "-s", "w", str(script)])

    cell, _ = _records(result)
    assert cell["session"] == "w"
    assert cell["diagnosis"]["next_actions"][0]["cmd"] == "colab install -s w torchvision"


def test_timeout_is_exit_4_and_still_tears_down(runtime, tmp_path, mock_common_state):
    script = tmp_path / "slow.py"
    script.write_text("while True: pass\n")

    def execute(code, output_hook=None, timeout=None):
        if code.startswith(CHDIR):
            return []
        raise TimeoutError("No output for 5 seconds.")

    runtime.execute_code.side_effect = execute

    result = runner.invoke(app, ["run", "--json", "--timeout", "5", str(script)])

    assert result.exit_code == 4
    cell, summary = _records(result)
    assert cell["diagnosis"]["code"] == "idle_timeout"
    assert summary["exit_code"] == 4
    mock_common_state.client.unassign.assert_called_once()


def _rate(mock_common_state, rate=2.0, balance=100.0):
    from colab_cli.consumption import ConsumptionUserInfo

    mock_common_state.client.get_consumption_user_info.return_value = ConsumptionUserInfo(
        paidComputeUnitsBalance=balance, consumptionRateHourly=rate, assignmentsCount=1
    )


def test_run_summary_has_a_cost_block(runtime, tmp_path, mock_common_state):
    _rate(mock_common_state)
    script = tmp_path / "ok.py"
    script.write_text("print(1)\n")
    runtime.execute_code.return_value = []

    result = runner.invoke(app, ["run", "--json", str(script)])

    summary = _records(result)[-1]
    assert summary["cost"]["rate_cu_per_hour"] == 2.0


def test_run_budget_stops_with_exit_5_and_tears_down(runtime, tmp_path, mock_common_state):
    from colab_cli.runtime import RunLimitExceeded

    _rate(mock_common_state)
    script = tmp_path / "long.py"
    script.write_text("train()\n")
    seen = {}

    def execute(code, **kw):
        if code.startswith(CHDIR):
            return []
        seen.update(kw)
        raise RunLimitExceeded("The run limit of 1800s was reached. The kernel was interrupted.")

    runtime.execute_code.side_effect = execute

    result = runner.invoke(app, ["run", "--json", "--budget", "1", str(script)])

    assert result.exit_code == 5
    assert 1790 <= seen["run_limit"] <= 1800
    cell, summary = _records(result)
    assert cell["diagnosis"]["code"] == "budget_reached"
    assert summary["exit_code"] == 5
    mock_common_state.client.unassign.assert_called_once()


@pytest.fixture
def job_kernel(mocker):
    rt = mocker.patch("colab_cli.commands.job.ColabRuntime").return_value

    def execute(code, **kw):
        jid = code.split("'/content/.colab-jobs/")[1].split("'")[0]
        return [{"output_type": "stream", "name": "stdout",
                 "text": "COLAB_JOB " + json.dumps({"id": jid, "pid": 5, "dir": "d"}) + "\n"}]

    rt.execute_code.side_effect = execute
    return rt


def test_detach_starts_a_job_and_keeps_the_runtime(runtime, job_kernel, tmp_path, mock_common_state):
    script = tmp_path / "train.py"
    script.write_text("print('x')\n")

    result = runner.invoke(app, ["run", "--detach", "-s", "bg", str(script), "--epochs", "3"])

    assert result.exit_code == 0, result.output
    code = job_kernel.execute_code.call_args.args[0]
    assert "'--epochs', '3'" in code
    # The script did not run in the kernel, and the runtime stays up.
    runtime.execute_code.assert_not_called()
    mock_common_state.client.unassign.assert_not_called()
    assert "colab job status -s bg train-" in result.output
    assert "colab stop -s bg" in result.output


def test_detach_json(runtime, job_kernel, tmp_path):
    script = tmp_path / "train.py"
    script.write_text("print('x')\n")

    result = runner.invoke(app, ["run", "--detach", "--json", "-s", "bg", str(script)])

    rec = json.loads(result.stdout)
    assert rec["type"] == "job" and rec["session"] == "bg" and rec["state"] == "starting"
