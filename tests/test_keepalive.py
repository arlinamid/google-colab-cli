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

import subprocess
from unittest.mock import MagicMock

from colab_cli import keepalive
from colab_cli.client import ColabRequestError


def _proc(waits):
    """A Popen stand-in whose wait() times out len(waits)-1 times, then returns."""
    proc = MagicMock()
    effects = [subprocess.TimeoutExpired("ssh", 1)] * (len(waits) - 1) + [waits[-1]]
    proc.wait.side_effect = effects
    return proc


def test_pings_while_process_runs_and_returns_its_exit_code():
    client = MagicMock()
    proc = _proc([None, None, 3])
    rc = keepalive.wait_with_keep_alive(proc, client, "ep1", interval=60)
    assert rc == 3
    # One ping up front, one per interval that elapsed while ssh ran.
    assert client.keep_alive_assignment.call_count == 3
    client.keep_alive_assignment.assert_called_with("ep1")
    proc.wait.assert_called_with(timeout=60)


def test_ping_errors_do_not_stop_the_process(capsys):
    client = MagicMock()
    client.keep_alive_assignment.side_effect = RuntimeError("network down")
    proc = _proc([None, None, 0])
    assert keepalive.wait_with_keep_alive(proc, client, "ep1", interval=60) == 0
    err = capsys.readouterr().err
    # Warned once, not on every failed ping.
    assert err.count("keep-alive") == 1


def test_session_gone_stops_pinging(capsys):
    client = MagicMock()
    response = MagicMock(status_code=404)
    client.keep_alive_assignment.side_effect = ColabRequestError(
        "gone", request=MagicMock(), response=response, response_body=""
    )
    proc = _proc([None, None, None, 0])
    assert keepalive.wait_with_keep_alive(proc, client, "ep1", interval=60) == 0
    assert client.keep_alive_assignment.call_count == 1
    assert "no longer exists" in capsys.readouterr().err


def test_ctrl_c_waits_for_child_and_returns_130():
    client = MagicMock()
    proc = MagicMock()
    proc.wait.side_effect = [KeyboardInterrupt(), 0]
    assert keepalive.wait_with_keep_alive(proc, client, "ep1", interval=60) == 130


def test_keep_alive_until_interrupted_pings_on_interval():
    client = MagicMock()
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    keepalive.keep_alive_until_interrupted(
        client, "ep1", interval=60, sleep=fake_sleep
    )
    assert client.keep_alive_assignment.call_count == 3
    assert sleeps == [60, 60, 60]


def test_keep_alive_until_interrupted_stops_after_duration():
    client = MagicMock()
    clock = iter([0, 0, 61, 122, 200])
    keepalive.keep_alive_until_interrupted(
        client,
        "ep1",
        interval=60,
        duration=120,
        sleep=lambda s: None,
        clock=lambda: next(clock),
    )
    assert client.keep_alive_assignment.call_count == 3
