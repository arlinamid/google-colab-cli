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

"""Cost feedback (docs/08, phase D): estimates, --budget, usage --json."""

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.consumption import ConsumptionUserInfo
from colab_cli.cost import CostMeter
from colab_cli.diagnosis import diagnose_failure
from colab_cli.runtime import ColabRuntime, RunLimitExceeded
from mock_kernel import MockKernelClient, connect_runtime, status_message, stdout_message

runner = CliRunner()


def _info(balance=100.0, rate=2.0, count=1):
    return ConsumptionUserInfo(
        paidComputeUnitsBalance=balance, consumptionRateHourly=rate, assignmentsCount=count
    )


def _client(info=None, error=None):
    client = MagicMock()
    if error:
        client.get_consumption_user_info.side_effect = error
    else:
        client.get_consumption_user_info.return_value = info or _info()
    return client


# ---- the meter ------------------------------------------------------------


def test_meter_estimates_from_the_hourly_rate():
    meter = CostMeter(_client(_info(balance=100.0, rate=2.0)), accelerator="T4")
    assert meter.seconds_for(1.0) == 1800.0
    assert meter.block(900) == {
        "accelerator": "T4",
        "rate_cu_per_hour": 2.0,
        "account_rate_cu_per_hour": 2.0,
        "rate_basis": "account",
        "balance_cu": 100.0,
        "used_cu_estimate": 0.5,
        "hours_left_at_rate": 50.0,
        "assignments": 1,
    }


def _assignments(*accelerators):
    from types import SimpleNamespace

    return [SimpleNamespace(accelerator=SimpleNamespace(value=a)) for a in accelerators]


class _Store:
    def __init__(self, rates=None):
        from colab_cli.state import Settings

        self.settings = Settings(cost_rates=rates or {})
        self.saved = 0

    def load(self):
        return self.settings

    def save(self, settings):
        self.settings = settings
        self.saved += 1


def test_alone_the_account_rate_is_this_runtime_and_is_learned():
    client = _client(_info(rate=0.08, count=1))
    client.list_assignments.return_value = _assignments("NONE")
    store = _Store()

    meter = CostMeter(client, accelerator="NONE", settings_store=store)

    assert meter.rate == 0.08 and meter.basis == "only_runtime"
    assert store.settings.cost_rates == {"NONE": 0.08}


def test_next_to_a_gpu_a_cpu_budget_uses_the_cpu_rate():
    """The demo bug: 0.05 CU on a CPU next to a T4 stopped after 2.4 minutes."""
    client = _client(_info(rate=1.23, count=2))
    client.list_assignments.return_value = _assignments("T4", "NONE")

    learned = CostMeter(client, accelerator="NONE", settings_store=_Store({"NONE": 0.075}))
    assert learned.rate == 0.075 and learned.basis == "learned"
    assert round(learned.seconds_for(0.05)) == 2400

    default = CostMeter(client, accelerator="NONE", settings_store=_Store())
    assert default.basis == "default" and default.rate == 0.08
    block = default.block(3600)
    assert block["rate_cu_per_hour"] == 0.08 and block["account_rate_cu_per_hour"] == 1.23


def test_an_unknown_rate_is_the_account_rate_minus_the_others():
    client = _client(_info(rate=7.0, count=2))
    client.list_assignments.return_value = _assignments("T4", "A100")
    meter = CostMeter(client, accelerator="A100", settings_store=_Store({"T4": 1.2}))
    assert meter.rate == pytest.approx(5.8) and meter.basis == "account_minus_others"

    client.list_assignments.return_value = _assignments("L4", "A100")  # L4 unknown too
    meter = CostMeter(client, accelerator="A100", settings_store=_Store({"T4": 1.2}))
    assert meter.rate == 7.0 and meter.basis == "account"


def test_learn_rates_only_from_a_single_runtime():
    from colab_cli.cost import learn_rates

    store = _Store()
    learn_rates(store, ["T4"], 1.12)
    learn_rates(store, ["T4", "NONE"], 1.2)
    assert store.settings.cost_rates == {"T4": 1.12} and store.saved == 1
    learn_rates(store, ["T4"], 1.12)
    assert store.saved == 1  # unchanged: not written again


def test_meter_without_consumption_info():
    meter = CostMeter(_client(error=RuntimeError("down")))
    assert meter.seconds_for(1.0) is None
    assert meter.block(10) is None


def test_meter_with_a_zero_rate_has_no_limit():
    meter = CostMeter(_client(_info(rate=0.0)))
    assert meter.seconds_for(5) is None
    assert meter.block(3600)["hours_left_at_rate"] is None


def test_budget_diagnosis():
    d = diagnose_failure("budget_reached", session="s", budget=0.5)
    assert d["code"] == "budget_reached"
    assert "0.5" in d["summary"]
    assert any(a["cmd"] == "colab usage --json" for a in d["next_actions"])


# ---- the runtime's run limit ------------------------------------------------


def test_run_limit_stops_code_that_keeps_printing(monkeypatch):
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MockKernelClient(
        [(t, stdout_message(f"{t}\n")) for t in range(5, 300, 5)] + [(300, status_message("idle"))]
    )
    connect_runtime(runtime, mock_kc, monkeypatch)
    with pytest.raises(RunLimitExceeded, match="60s"):
        runtime.execute_code("work()", output_hook=lambda o: None, run_limit=60)
    runtime._kernel_client.interrupt.assert_called_once()


def test_run_limit_not_reached(monkeypatch):
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MockKernelClient([(5, stdout_message("x\n")), (6, status_message("idle"))])
    connect_runtime(runtime, mock_kc, monkeypatch)
    runtime.execute_code("work()", output_hook=lambda o: None, run_limit=60)
    runtime._kernel_client.interrupt.assert_not_called()


# ---- commands ---------------------------------------------------------------


@pytest.fixture
def exec_session(mock_common_state, mocker):
    s = MagicMock(url="http://u", token="t", kernel_id=None, session_id=None, accelerator="T4")
    s.name = "s1"
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    mock_common_state.client.get_consumption_user_info.return_value = _info(balance=100.0, rate=2.0)
    return mocker.patch("colab_cli.commands.execution.ColabRuntime").return_value


def _records(result):
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_exec_json_summary_has_a_cost_block(exec_session):
    exec_session.execute_code.return_value = []

    result = runner.invoke(app, ["exec", "-s", "s1", "--json"], input="print(1)\n")

    summary = _records(result)[-1]
    assert summary["cost"]["rate_cu_per_hour"] == 2.0
    assert summary["cost"]["accelerator"] == "T4"
    assert summary["cost"]["used_cu_estimate"] >= 0


def test_exec_budget_becomes_a_run_limit_and_exit_5(exec_session):
    calls = []

    def execute(code, **kw):
        calls.append(kw)
        if code.startswith("import os; os.makedirs('/content'"):
            return []
        raise RunLimitExceeded("The run limit of 1800s was reached. The kernel was interrupted.")

    exec_session.execute_code.side_effect = execute

    result = runner.invoke(app, ["exec", "-s", "s1", "--json", "--budget", "1"], input="train()\n")

    assert result.exit_code == 5
    # 1 CU at 2 CU/h is 30 minutes for the whole exec.
    assert 1790 <= calls[-1]["run_limit"] <= 1800
    cell, summary = _records(result)
    assert cell["diagnosis"]["code"] == "budget_reached"
    assert summary["exit_code"] == 5


def test_exec_budget_without_json_prints_and_exits_5(exec_session):
    def execute(code, **kw):
        if code.startswith("import os; os.makedirs('/content'"):
            return []
        raise RunLimitExceeded("limit")

    exec_session.execute_code.side_effect = execute

    result = runner.invoke(app, ["exec", "-s", "s1", "--budget", "1"], input="train()\n")

    assert result.exit_code == 5
    assert "budget" in result.output.lower()


def test_usage_json(mock_common_state):
    mock_common_state.client.get_consumption_user_info.return_value = _info(balance=50.0, rate=5.0, count=2)

    result = runner.invoke(app, ["usage", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "schema": "colab.v1",
        "type": "usage",
        "balance_cu": 50.0,
        "rate_cu_per_hour": 5.0,
        "hours_left_at_rate": 10.0,
        "assignments": 2,
    }
