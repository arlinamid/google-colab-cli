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

import re

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import share as share_cmd
from colab_cli.state import SessionState

runner = CliRunner()


@pytest.fixture
def runtime(mock_common_state, mocker, monkeypatch):
    s = SessionState(name="s1", token="t", url="https://u", endpoint="ep1")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    rt = mocker.patch("colab_cli.commands.automation.ColabRuntime").return_value
    rt.execute_code.return_value = [{"text": "Public URL: https://x.trycloudflare.com\n"}]
    return rt


@pytest.fixture
def keep_alive(mocker):
    return mocker.patch.object(share_cmd, "keep_alive_until_interrupted")


def _codes(runtime):
    return [c.args[0] for c in runtime.execute_code.call_args_list]


def test_cloudflared_default_starts_keeps_alive_then_stops(
    runtime, keep_alive, mock_common_state
):
    result = runner.invoke(app, ["share", "-s", "s1", "8000"])
    assert result.exit_code == 0, result.output
    start, stop = _codes(runtime)
    assert "https://pkg.cloudflare.com/cloudflared any main" in start
    assert "signed-by=/usr/share/keyrings/cloudflare-main.gpg" in start
    assert "['cloudflared', 'tunnel', '--url', 'http://localhost:8000', '--no-autoupdate']" in start
    assert "trycloudflare" in start
    keep_alive.assert_called_once_with(mock_common_state.client, "ep1")
    assert "_stop(8000)" in stop and "killpg" in stop
    assert "publicly reachable" in result.output


def test_install_runs_with_a_long_timeout(runtime, keep_alive):
    runner.invoke(app, ["share", "-s", "s1", "8000"])
    assert runtime.execute_code.call_args_list[0].kwargs["timeout"] >= 300


def test_detach_leaves_tunnel_running_without_keep_alive(runtime, keep_alive):
    result = runner.invoke(app, ["share", "-s", "s1", "8000", "--detach"])
    assert result.exit_code == 0, result.output
    assert len(_codes(runtime)) == 1
    keep_alive.assert_not_called()
    assert "colab keepalive" in result.output


def test_ngrok_token_goes_to_env_not_argv_and_is_redacted(
    runtime, keep_alive, mock_common_state, monkeypatch
):
    monkeypatch.setenv("NGROK_AUTHTOKEN", "ngrok_secret_tok")
    result = runner.invoke(app, ["share", "-s", "s1", "7860", "--via", "ngrok"])
    assert result.exit_code == 0, result.output
    start = _codes(runtime)[0]
    assert "https://ngrok-agent.s3.amazonaws.com" in start
    assert "['ngrok', 'http', '7860', '--log', 'stdout']" in start
    argv_line = re.search(r"_cmd = (\[.*\])", start).group(1)
    assert "ngrok_secret_tok" not in argv_line
    assert "'NGROK_AUTHTOKEN': 'ngrok_secret_tok'" in start
    mock_common_state.history.add_redaction.assert_any_call("ngrok_secret_tok")
    assert "ngrok_secret_tok" not in result.output


def test_ngrok_without_token_exits_2_before_runtime(runtime, keep_alive, mocker):
    mocker.patch("getpass.getpass", side_effect=EOFError)
    result = runner.invoke(app, ["share", "-s", "s1", "7860", "--via", "ngrok"])
    assert result.exit_code == 2
    runtime.execute_code.assert_not_called()


def test_ngrok_token_from_prompt(runtime, keep_alive, mocker):
    mocker.patch("colab_cli.secrets._can_prompt", return_value=True)
    mocker.patch("getpass.getpass", return_value="typed_ngrok")
    result = runner.invoke(
        app, ["share", "-s", "s1", "7860", "--via", "ngrok", "--token", "prompt"]
    )
    assert result.exit_code == 0, result.output
    assert "'NGROK_AUTHTOKEN': 'typed_ngrok'" in _codes(runtime)[0]


@pytest.mark.parametrize("port", ["0", "65536", "abc"])
def test_bad_port_exits_2(runtime, keep_alive, port):
    result = runner.invoke(app, ["share", "-s", "s1", port])
    assert result.exit_code == 2
    runtime.execute_code.assert_not_called()


def test_unknown_tool_exits_2(runtime, keep_alive):
    result = runner.invoke(app, ["share", "-s", "s1", "8000", "--via", "bore"])
    assert result.exit_code == 2


def test_start_failure_exits_1_without_keep_alive(runtime, keep_alive):
    runtime.execute_code.return_value = [
        {"output_type": "error", "ename": "RuntimeError", "evalue": "no url", "traceback": []}
    ]
    result = runner.invoke(app, ["share", "-s", "s1", "8000"])
    assert result.exit_code == 1
    keep_alive.assert_not_called()


def test_list_and_stop(runtime, keep_alive):
    result = runner.invoke(app, ["share", "-s", "s1", "--list"])
    assert result.exit_code == 0, result.output
    assert "colab-share-" in _codes(runtime)[-1]

    result = runner.invoke(app, ["share", "-s", "s1", "--stop", "8000"])
    assert result.exit_code == 0, result.output
    stop = _codes(runtime)[-1]
    assert "killpg" in stop and "8000" in stop

    result = runner.invoke(app, ["share", "-s", "s1", "--stop"])
    assert result.exit_code == 0, result.output
    assert "None" in _codes(runtime)[-1]  # every port
    keep_alive.assert_not_called()


def test_port_required_to_start(runtime, keep_alive):
    result = runner.invoke(app, ["share", "-s", "s1"])
    assert result.exit_code == 2


def test_keepalive_command(runtime, keep_alive, mock_common_state):
    result = runner.invoke(app, ["keepalive", "-s", "s1", "--minutes", "30"])
    assert result.exit_code == 0, result.output
    keep_alive.assert_called_once_with(mock_common_state.client, "ep1", duration=1800)


def test_keepalive_command_until_ctrl_c(runtime, keep_alive, mock_common_state):
    result = runner.invoke(app, ["keepalive", "-s", "s1"])
    assert result.exit_code == 0, result.output
    keep_alive.assert_called_once_with(mock_common_state.client, "ep1", duration=None)
