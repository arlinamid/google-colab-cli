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

"""Nothing should be left running: orphaned runtimes, ssh clients, proxies."""

import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module

runner = CliRunner()


def _assignment(endpoint):
    a = MagicMock(endpoint=endpoint)
    a.accelerator.value = "NONE"
    a.variant.name = "DEFAULT"
    a.machine_shape.name = "STANDARD"
    return a


@pytest.fixture
def server(mock_common_state):
    """One locally known session (`mine`) and two server-side orphans."""
    mine = MagicMock(endpoint="ep-mine")
    mine.name = "mine"
    assignments = [_assignment("ep-mine"), _assignment("ep-a"), _assignment("ep-b")]
    mock_common_state.sync_sessions.return_value = ({"mine": mine}, assignments)
    return mock_common_state


# ---------- colab stop --orphans ------------------------------------------


def test_orphans_listed_and_stopped_after_confirmation(server):
    result = runner.invoke(app, ["stop", "--orphans"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "ep-a" in result.output and "ep-b" in result.output
    unassigned = [c.args[0] for c in server.client.unassign.call_args_list]
    assert unassigned == ["ep-a", "ep-b"]
    assert "Colab web UI" in result.output  # warns what an orphan may be


def test_orphans_kept_when_not_confirmed(server):
    result = runner.invoke(app, ["stop", "--orphans"], input="n\n")
    assert result.exit_code == 0
    server.client.unassign.assert_not_called()


def test_orphans_yes_skips_the_prompt(server):
    result = runner.invoke(app, ["stop", "--orphans", "--yes"])
    assert result.exit_code == 0, result.output
    assert server.client.unassign.call_count == 2


def test_orphans_never_touch_local_sessions(server):
    runner.invoke(app, ["stop", "--orphans", "--yes"])
    assert "ep-mine" not in [c.args[0] for c in server.client.unassign.call_args_list]


def test_no_orphans(mock_common_state):
    mine = MagicMock(endpoint="ep-mine")
    mine.name = "mine"
    mock_common_state.sync_sessions.return_value = (
        {"mine": mine},
        [_assignment("ep-mine")],
    )
    result = runner.invoke(app, ["stop", "--orphans"])
    assert result.exit_code == 0
    assert "No orphaned" in result.output
    mock_common_state.client.unassign.assert_not_called()


def test_orphans_and_session_are_exclusive(server):
    result = runner.invoke(app, ["stop", "-s", "mine", "--orphans"])
    assert result.exit_code == 2
    server.client.unassign.assert_not_called()


def test_one_failed_unassign_does_not_stop_the_rest(server):
    server.client.unassign.side_effect = [RuntimeError("boom"), None]
    result = runner.invoke(app, ["stop", "--orphans", "--yes"])
    assert server.client.unassign.call_count == 2
    assert result.exit_code == 1
    assert "boom" in result.output


# ---------- the proxy's websocket connect ---------------------------------


def test_websocket_connect_is_bounded_then_unlimited(mocker):
    """A connect that never completes must not hang the ProxyCommand forever,
    but an idle ssh session must not time out afterwards."""
    ws = MagicMock()
    mocker.patch("websocket.WebSocket", return_value=ws)
    ssh_module._connect_websocket("wss://x/colab/ssh", "pk")
    assert ws.connect.call_args.kwargs["timeout"] == ssh_module._CONNECT_TIMEOUT_SEC
    ws.settimeout.assert_called_once_with(None)


# ---------- ssh dies with colab on Windows --------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_children_die_when_colab_is_killed():
    """Seen live: after the colab process was killed, ssh.exe kept running."""
    parent_code = (
        "import subprocess, sys, time\n"
        "from colab_cli.commands import ssh\n"
        "ssh._kill_children_with_this_process()\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True
    )
    child_pid = int(parent.stdout.readline())
    assert _alive(child_pid)
    parent.kill()  # TerminateProcess: no cleanup code runs in the parent
    parent.wait(timeout=30)
    deadline = time.time() + 10
    while _alive(child_pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not _alive(child_pid)


def _alive(pid):
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    return str(pid) in out
