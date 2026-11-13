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

"""Finding open `colab forward` / `colab ssh -L` tunnels on this computer (docs/09)."""

import os
import shlex
import socket
import subprocess

from colab_cli.forwards import parse_ssh_command, port_listening, scan_forwards

WIN = (
    r'C:\Windows\System32\OpenSSH\ssh.exe -o "ProxyCommand=\"C:\Users\me\.local\bin\colab.exe\" '
    r'ssh --proxy-mode -s webui --identity C:\Users\me\.config\webui\ssh_key '
    r'--colab-internal-mode forward" -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL '
    r'-o LogLevel=ERROR -i C:\Users\me\.config\webui\ssh_key -o ExitOnForwardFailure=yes '
    r'-N -L 8188:localhost:8188 root@colab'
)


def test_windows_command_line():
    assert parse_ssh_command(WIN) == {
        "session": "webui", "config": None,
        "ports": [{"local_port": 8188, "remote": "localhost:8188"}],
    }


def test_posix_command_line_with_config_and_quoted_name():
    proxy = shlex.join(["/usr/bin/python3", "-m", "colab_cli.cli", "--config", "/tmp/x/s.json",
                        "ssh", "--proxy-mode", "-s", "my box"])
    # As `ps` prints it: the argv joined with spaces, unquoted.
    cmd = " ".join(["ssh", "-o", f"ProxyCommand={proxy}", "-L", "7860:localhost:7860",
                    "-L", "8080:127.0.0.1:80", "root@colab"])
    info = parse_ssh_command(cmd)
    assert info["session"] == "my box" and info["config"] == "/tmp/x/s.json"
    assert [p["local_port"] for p in info["ports"]] == [7860, 8080]
    assert info["ports"][1]["remote"] == "127.0.0.1:80"


def test_not_a_colab_forward():
    assert parse_ssh_command("ssh -L 8000:localhost:8000 me@host") is None
    no_forward = WIN.replace(" -L 8188:localhost:8188", "")
    assert parse_ssh_command(no_forward) is None


def test_scan_groups_by_session_and_checks_the_port(tmp_path):
    cfg = str(tmp_path / "s.json")
    other = WIN.replace("ssh --proxy-mode -s webui", f"--config {cfg} ssh --proxy-mode -s other")
    assert parse_ssh_command(other)["config"] == cfg

    found = scan_forwards(None, lister=lambda: [WIN, WIN, other], listening=lambda p: p == 8188)
    assert found == {"webui": [{"local_port": 8188, "remote": "localhost:8188", "listening": True}]}

    mine = scan_forwards(cfg, lister=lambda: [WIN, other], listening=lambda p: False)
    assert list(mine) == ["other"] and mine["other"][0]["listening"] is False


def test_port_listening():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    try:
        assert port_listening(port)
    finally:
        server.close()
    assert not port_listening(port)


def test_list_ssh_commands_survives_errors(mocker):
    from colab_cli import forwards

    mocker.patch.object(forwards.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1))
    assert forwards.list_ssh_commands("win32") == []
    assert forwards.list_ssh_commands("darwin") == []


def test_list_ssh_commands_on_this_os_runs():
    from colab_cli.forwards import list_ssh_commands

    assert isinstance(list_ssh_commands(), list)
    assert os.name  # no exception on this platform
