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

"""Port forwarding: `colab ssh -L SPEC` and `colab forward PORT...`."""

from unittest.mock import MagicMock

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module
from colab_cli.common import State
import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def one_session(mock_common_state, mocker):
    session = MagicMock(url="https://abc.colab.googleusercontent.com", token="TOK")
    session.name = "s1"
    mock_common_state.store.get.return_value = session
    mock_common_state.store.list.return_value = {"s1": session}
    mock_common_state.sync_sessions.return_value = ({"s1": session}, [])
    mock_common_state.resolve_session.side_effect = (
        lambda name: State.resolve_session(mock_common_state, name)
    )
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    # The update check opens a filelock that misbehaves under the faked
    # platform; `colab ssh` skips it when piped, `colab forward` does not.
    mocker.patch("colab_cli.cli.auto_update.run_background_check")
    return mock_common_state


@pytest.fixture
def popen(mocker):
    """ssh with forwards runs under Popen so the CLI can ping keep-alive."""
    p = mocker.patch("subprocess.Popen")
    p.return_value.wait.return_value = 0
    return p


def _forwards(args):
    return [args[i + 1] for i, a in enumerate(args) if a == "-L"]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("7860", "7860:localhost:7860"),
        ("8080:8888", "8080:localhost:8888"),
        ("9000:127.0.0.1:6006", "9000:127.0.0.1:6006"),
    ],
)
def test_parse_forward(spec, expected):
    assert ssh_module._parse_forward(spec) == expected


@pytest.mark.parametrize("spec", ["0", "70000", "abc", "1:2:3:4", "80:", ":80", ""])
def test_parse_forward_rejects_bad_specs(spec):
    import typer

    with pytest.raises(typer.Exit) as exc_info:
        ssh_module._parse_forward(spec)
    assert exc_info.value.exit_code == 2


def test_interactive_ssh_adds_forwards(popen):
    call = popen
    result = runner.invoke(app, ["ssh", "-s", "s1", "-L", "7860", "-L", "8080:8888"])
    assert result.exit_code == 0, result.output
    args = call.call_args.args[0]
    assert _forwards(args) == ["7860:localhost:7860", "8080:localhost:8888"]
    assert "ExitOnForwardFailure=yes" in args
    assert "-t" in args


def test_no_shell_forward_only(popen):
    call = popen
    result = runner.invoke(app, ["ssh", "-s", "s1", "-N", "-L", "7860"])
    assert result.exit_code == 0, result.output
    args = call.call_args.args[0]
    assert "-N" in args
    assert args[-1] == ssh_module._SSH_HOST
    assert "http://localhost:7860" in result.output


def test_no_shell_without_forward_is_an_error(mocker, popen):
    call = mocker.patch("subprocess.call", return_value=0)
    result = runner.invoke(app, ["ssh", "-s", "s1", "-N"])
    assert result.exit_code == 2
    call.assert_not_called()
    popen.assert_not_called()


def test_cmd_mode_keeps_forwards(popen):
    call = popen
    result = runner.invoke(app, ["ssh", "-s", "s1", "-L", "8000", "--", "echo", "hi"])
    assert result.exit_code == 0, result.output
    assert _forwards(call.call_args.args[0]) == ["8000:localhost:8000"]


def test_forwards_without_ssh_have_no_exit_on_failure_option(mocker):
    call = mocker.patch("subprocess.call", return_value=0)
    runner.invoke(app, ["ssh", "-s", "s1"])
    args = call.call_args.args[0]
    assert "-L" not in args
    assert "ExitOnForwardFailure=yes" not in args


def test_forward_command_is_ssh_no_shell(popen):
    call = popen
    result = runner.invoke(app, ["forward", "-s", "s1", "7860", "6006"])
    assert result.exit_code == 0, result.output
    args = call.call_args.args[0]
    assert "-N" in args
    assert _forwards(args) == ["7860:localhost:7860", "6006:localhost:6006"]


def test_forward_command_bad_port_exits_2(popen):
    result = runner.invoke(app, ["forward", "-s", "s1", "99999"])
    assert result.exit_code == 2
    popen.assert_not_called()


def test_forward_pings_keep_alive_for_the_session(popen, one_session):
    session = one_session.store.get.return_value
    session.endpoint = "ep-123"
    result = runner.invoke(app, ["forward", "-s", "s1", "7860"])
    assert result.exit_code == 0, result.output
    one_session.client.keep_alive_assignment.assert_called_with("ep-123")


def test_no_keep_alive_runs_ssh_directly(mocker, popen, one_session):
    call = mocker.patch("subprocess.call", return_value=0)
    result = runner.invoke(app, ["forward", "-s", "s1", "--no-keep-alive", "7860"])
    assert result.exit_code == 0, result.output
    popen.assert_not_called()
    assert _forwards(call.call_args.args[0]) == ["7860:localhost:7860"]
    one_session.client.keep_alive_assignment.assert_not_called()


def test_forward_exit_code_is_ssh_exit_code(popen):
    popen.return_value.wait.return_value = 255
    result = runner.invoke(app, ["forward", "-s", "s1", "7860"])
    assert result.exit_code == 255


def test_proxy_mode_ignores_forwards(mocker):
    bridge = mocker.patch.object(ssh_module, "_run_proxy_bridge", return_value=0)
    result = runner.invoke(app, ["ssh", "--proxy-mode", "-s", "s1", "-L", "7860"])
    assert result.exit_code == 0
    assert "ignored" in result.output
    bridge.assert_called_once()
