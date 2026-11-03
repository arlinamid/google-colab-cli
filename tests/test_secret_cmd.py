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

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.state import SessionState

runner = CliRunner()


@pytest.fixture
def runtime(mock_common_state, mocker):
    s = SessionState(name="s1", token="t", url="https://u", endpoint="e1")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    runtime_class = mocker.patch("colab_cli.commands.automation.ColabRuntime")
    rt = runtime_class.return_value
    rt.execute_code.return_value = [{"text": "Set: HF_TOKEN\n"}]
    return rt


def test_secret_set_injects_value_and_redacts(runtime, mock_common_state, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_cmd_secret")
    result = runner.invoke(app, ["secret", "set", "-s", "s1", "HF_TOKEN"])
    assert result.exit_code == 0, result.output
    code = runtime.execute_code.call_args.args[0]
    assert "os.environ['HF_TOKEN'] = 'hf_cmd_secret'" in code
    mock_common_state.history.add_redaction.assert_called_with("hf_cmd_secret")
    assert "hf_cmd_secret" not in result.output


def test_secret_set_multiple_and_sources(runtime, monkeypatch, mocker):
    monkeypatch.setenv("LOCAL_A", "aaa111")
    mocker.patch("colab_cli.secrets._can_prompt", return_value=True)
    mocker.patch("getpass.getpass", return_value="bbb222")
    result = runner.invoke(
        app, ["secret", "set", "-s", "s1", "A=env:LOCAL_A", "B=prompt"]
    )
    assert result.exit_code == 0, result.output
    code = runtime.execute_code.call_args.args[0]
    assert "os.environ['A'] = 'aaa111'" in code
    assert "os.environ['B'] = 'bbb222'" in code


def test_secret_set_missing_value_never_touches_runtime(runtime, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    result = runner.invoke(app, ["secret", "set", "-s", "s1", "HF_TOKEN"])
    assert result.exit_code == 2
    runtime.execute_code.assert_not_called()


def test_secret_set_kernel_error_exits_1(runtime, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "x1y2z3")
    runtime.execute_code.return_value = [
        {"output_type": "error", "ename": "E", "evalue": "boom", "traceback": []}
    ]
    result = runner.invoke(app, ["secret", "set", "-s", "s1", "HF_TOKEN"])
    assert result.exit_code == 1


def test_secret_set_targets_the_session_kernel(mock_common_state, mocker, monkeypatch):
    """`colab exec` reuses the session's kernel; the secret must land there."""
    s = SessionState(
        name="s1", token="t", url="https://u", endpoint="e1",
        kernel_id="k-1", session_id="sess-1",
    )
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    runtime_class = mocker.patch("colab_cli.commands.automation.ColabRuntime")
    runtime_class.return_value.execute_code.return_value = []
    monkeypatch.setenv("HF_TOKEN", "abc123")

    result = runner.invoke(app, ["secret", "set", "-s", "s1", "HF_TOKEN"])

    assert result.exit_code == 0, result.output
    kwargs = runtime_class.call_args.kwargs
    assert kwargs["kernel_id"] == "k-1"
    assert kwargs["session_id"] == "sess-1"


def test_install_still_uses_a_fresh_kernel(mock_common_state, mocker):
    s = SessionState(name="s1", token="t", url="https://u", endpoint="e1", kernel_id="k-1")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    runtime_class = mocker.patch("colab_cli.commands.automation.ColabRuntime")
    runtime_class.return_value.execute_code.return_value = []
    runner.invoke(app, ["install", "-s", "s1", "pandas"])
    assert "kernel_id" not in runtime_class.call_args.kwargs
