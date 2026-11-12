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

"""`colab job start|status|tail|wait|stop|list` with a fake runtime (phase B)."""

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import job as job_cmd
from colab_cli.jobs import JOBS_DIR

runner = CliRunner()


class FakeFiles:
    """In-memory stand-in for ContentsClient: path -> text."""

    def __init__(self):
        self.files = {}

    def read_text(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_text(self, path, text):
        self.files[path] = text

    def list_dir(self, path):
        names = sorted({p[len(path) + 1:].split("/")[0] for p in self.files if p.startswith(path + "/")})
        if not names:
            raise FileNotFoundError(path)
        return {"type": "directory", "content": [{"name": n, "type": "directory"} for n in names]}


def _put_job(files, job_id, state="running", started=1000.0, **extra):
    status = {
        "id": job_id, "name": "train", "argv": ["python", "x.py"], "state": state,
        "pid": 7, "started_at": started, "ended_at": None, "exit_code": None,
        "last_output_at": started + 10, "progress": {"percent": 50, "n": 5, "total": 10, "eta_s": 60},
    }
    status.update(extra)
    base = f"{JOBS_DIR}/{job_id}"
    files.files[f"{base}/status.json"] = json.dumps(status)
    files.files[f"{base}/tail.txt"] = "epoch 1\nepoch 2\n"
    files.files[f"{base}/metrics.jsonl"] = json.dumps({"t": 1, "step": 2, "loss": 0.5}) + "\n"


@pytest.fixture
def files(mocker, mock_common_state):
    f = FakeFiles()
    mocker.patch.object(job_cmd, "ContentsClient", return_value=f)
    s = MagicMock(url="http://u", token="t", kernel_id=None, session_id=None, endpoint="ep")
    s.name = "s1"
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    mocker.patch.object(job_cmd.time, "time", return_value=1100.0)
    return f


@pytest.fixture
def kernel(mocker):
    rt = mocker.patch.object(job_cmd, "ColabRuntime").return_value

    def execute(code, **kw):
        jid = code.split("'/content/.colab-jobs/")[1].split("'")[0]
        return [{"output_type": "stream", "name": "stdout",
                 "text": "COLAB_JOB " + json.dumps({"id": jid, "pid": 99, "dir": f"{JOBS_DIR}/{jid}"}) + "\n"}]

    rt.execute_code.side_effect = execute
    return rt


def test_start_a_script(files, kernel, tmp_path, mock_common_state):
    script = tmp_path / "train.py"
    script.write_text("print('x')\n")

    result = runner.invoke(app, ["job", "start", "-s", "s1", "--name", "train",
                                 "--env", "LR=1e-4", "-f", str(script), "--", "--epochs", "3"])

    assert result.exit_code == 0, result.output
    code = kernel.execute_code.call_args.args[0]
    assert "'LR': '1e-4'" in code
    assert "'--epochs', '3'" in code
    assert "Started job train-" in result.output
    assert "colab job status -s s1 train-" in result.output
    events = [c.args[1] for c in mock_common_state.history.log_event.call_args_list]
    assert "job_started" in events


def test_start_a_command_json(files, kernel):
    result = runner.invoke(app, ["job", "start", "-s", "s1", "--json", "--", "nvidia-smi -l 5"])

    assert result.exit_code == 0, result.output
    rec = json.loads(result.stdout)
    assert rec["type"] == "job" and rec["state"] == "starting"
    assert rec["argv"] == ["bash", "-c", "nvidia-smi -l 5"]
    assert rec["pid"] == 99


def test_start_needs_a_script_or_a_command(files, kernel):
    result = runner.invoke(app, ["job", "start", "-s", "s1"])
    assert result.exit_code == 2


def test_status_json(files):
    _put_job(files, "train-20261112-090000-abcd")

    result = runner.invoke(app, ["job", "status", "-s", "s1", "train-20261112-090000-abcd", "--json"])

    assert result.exit_code == 0, result.output
    rec = json.loads(result.stdout)
    assert rec["state"] == "running"
    assert rec["progress"]["percent"] == 50
    assert rec["metrics"]["last"] == {"step": 2, "loss": 0.5}
    assert rec["tail"] == ["epoch 1", "epoch 2"]
    assert rec["session"] == "s1"


def test_status_defaults_to_the_newest_job(files):
    _put_job(files, "a-20261112-080000-0000")
    _put_job(files, "b-20261112-090000-0000")

    result = runner.invoke(app, ["job", "status", "-s", "s1", "--json"])

    assert json.loads(result.stdout)["id"] == "b-20261112-090000-0000"


def test_status_human(files):
    _put_job(files, "j-20261112-090000-0000")

    result = runner.invoke(app, ["job", "status", "-s", "s1"])

    assert "j-20261112-090000-0000: running" in result.output
    assert "50%" in result.output
    assert "loss=0.5" in result.output


def test_unknown_job(files):
    result = runner.invoke(app, ["job", "status", "-s", "s1", "nope"])
    assert result.exit_code == 1
    assert "nope" in result.output


def test_tail(files):
    _put_job(files, "j-20261112-090000-0000")

    result = runner.invoke(app, ["job", "tail", "-s", "s1", "j-20261112-090000-0000", "-n", "1"])

    assert result.stdout == "epoch 2\n"


def test_stop_writes_the_stop_file(files):
    _put_job(files, "j-20261112-090000-0000")

    result = runner.invoke(app, ["job", "stop", "-s", "s1", "j-20261112-090000-0000"])

    assert result.exit_code == 0
    assert f"{JOBS_DIR}/j-20261112-090000-0000/stop" in files.files


def test_list_json(files):
    _put_job(files, "a-20261112-080000-0000", state="succeeded", exit_code=0, ended_at=1050.0)
    _put_job(files, "b-20261112-090000-0000")

    result = runner.invoke(app, ["job", "list", "-s", "s1", "--json"])

    recs = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(r["id"], r["state"]) for r in recs] == [
        ("a-20261112-080000-0000", "succeeded"), ("b-20261112-090000-0000", "running"),
    ]


def test_list_without_jobs(files):
    result = runner.invoke(app, ["job", "list", "-s", "s1"])
    assert result.exit_code == 0
    assert "No jobs" in result.output


def test_wait_polls_pings_and_returns_the_exit_code(files, mocker):
    jid = "j-20261112-090000-0000"
    _put_job(files, jid)
    pinger = mocker.patch.object(job_cmd, "_Pinger").return_value
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        _put_job(files, jid, state="failed", exit_code=2, ended_at=1090.0)

    mocker.patch.object(job_cmd.time, "sleep", side_effect=fake_sleep)

    result = runner.invoke(app, ["job", "wait", "-s", "s1", jid, "--interval", "5", "--json"])

    assert result.exit_code == 2
    assert sleeps == [5]
    pinger.ping.assert_called()
    rec = json.loads(result.stdout)
    assert rec["state"] == "failed"


def test_wait_timeout_is_exit_4(files, mocker):
    jid = "j-20261112-090000-0000"
    _put_job(files, jid)
    mocker.patch.object(job_cmd, "_Pinger")
    clock = iter([0, 0, 3, 6, 9, 12])
    mocker.patch.object(job_cmd.time, "monotonic", side_effect=lambda: next(clock))
    mocker.patch.object(job_cmd.time, "sleep")

    result = runner.invoke(app, ["job", "wait", "-s", "s1", jid, "--interval", "3", "--timeout", "5"])

    assert result.exit_code == 4
    assert "still running" in result.output


def test_newest_job_is_by_time_not_by_name(files):
    _put_job(files, "zeta-20261112-080000-0000")
    _put_job(files, "alpha-20261112-090000-0000")

    result = runner.invoke(app, ["job", "status", "-s", "s1", "--json"])

    assert json.loads(result.stdout)["id"] == "alpha-20261112-090000-0000"


def test_list_is_in_time_order_without_guessing_diagnoses(files):
    _put_job(files, "train-20261112-090000-0000", state="succeeded", exit_code=0, ended_at=1050.0)
    _put_job(files, "bash-20261112-091000-0000", state="failed", exit_code=1, ended_at=1060.0)

    result = runner.invoke(app, ["job", "list", "-s", "s1", "--json"])

    recs = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in recs] == ["train-20261112-090000-0000", "bash-20261112-091000-0000"]
    # list reads only status.json, so it cannot know why a job failed.
    assert recs[1]["diagnosis"] is None


def test_start_budget_sets_max_seconds(files, kernel, mock_common_state):
    from colab_cli.consumption import ConsumptionUserInfo

    mock_common_state.client.get_consumption_user_info.return_value = ConsumptionUserInfo(
        paidComputeUnitsBalance=100.0, consumptionRateHourly=2.0, assignmentsCount=1
    )

    result = runner.invoke(app, ["job", "start", "-s", "s1", "--budget", "1", "--", "sleep 9999"])

    assert result.exit_code == 0, result.output
    assert "'max_seconds': 1800.0" in kernel.execute_code.call_args.args[0]


def test_status_json_has_a_cost_estimate(files, mock_common_state):
    from colab_cli.consumption import ConsumptionUserInfo

    mock_common_state.client.get_consumption_user_info.return_value = ConsumptionUserInfo(
        paidComputeUnitsBalance=100.0, consumptionRateHourly=3.6, assignmentsCount=1
    )
    _put_job(files, "j-20261112-090000-0000")  # started 1000, now 1100

    result = runner.invoke(app, ["job", "status", "-s", "s1", "j-20261112-090000-0000", "--json"])

    assert json.loads(result.stdout)["cost"]["used_cu_estimate"] == 0.1
