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

"""Which ssh client runs, and prompting without a console.

Under Git Bash the first `ssh` on PATH is MSYS ssh, which runs the
ProxyCommand through /bin/bash and strips the backslashes of the Windows
python path ("exec: C:Users...python.exe: not found"). The ProxyCommand is
quoted for Windows OpenSSH, so that is the client to run on Windows.
"""

import os
import subprocess
from unittest.mock import MagicMock

import pytest
import typer

from colab_cli.commands import ssh as ssh_module

OPENSSH = os.path.join(r"C:\Windows", "System32", "OpenSSH")


@pytest.fixture
def windows(mocker, monkeypatch):
    mocker.patch.object(ssh_module.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")


def test_windows_prefers_openssh_over_path(windows, mocker):
    mocker.patch.object(
        ssh_module.os.path, "isfile", side_effect=lambda p: p.startswith(OPENSSH)
    )
    assert ssh_module._openssh_tool("ssh") == os.path.join(OPENSSH, "ssh.exe")
    assert ssh_module._openssh_tool("ssh-keygen") == os.path.join(
        OPENSSH, "ssh-keygen.exe"
    )


def test_windows_without_openssh_falls_back_to_path(windows, mocker):
    mocker.patch.object(ssh_module.os.path, "isfile", return_value=False)
    assert ssh_module._openssh_tool("ssh") == "ssh"


def test_posix_uses_path_ssh(mocker):
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    assert ssh_module._openssh_tool("ssh") == "ssh"
    assert ssh_module._openssh_tool("ssh-keygen") == "ssh-keygen"


def test_ssh_command_uses_the_resolved_client(windows, mocker):
    mocker.patch.object(
        ssh_module.os.path, "isfile", side_effect=lambda p: p.startswith(OPENSSH)
    )
    args = ssh_module._ssh_base_args("proxy", None)
    assert args[0] == os.path.join(OPENSSH, "ssh.exe")


def test_ssh_keygen_uses_the_resolved_tool(windows, mocker, tmp_path):
    mocker.patch.object(
        ssh_module.os.path, "isfile", side_effect=lambda p: p.startswith(OPENSSH)
    )
    run = mocker.patch(
        "subprocess.run", return_value=MagicMock(stdout="ssh-ed25519 AAAA x\n")
    )
    ssh_module._run_ssh_keygen(str(tmp_path / "k"), "")
    assert run.call_args.args[0][0] == os.path.join(OPENSSH, "ssh-keygen.exe")


def test_passphrase_prompt_without_console_fails_fast(mocker, tmp_path, capsys):
    """Seen live: a key with a passphrase hung `colab ssh` and `colab forward`,
    because the ProxyCommand child waited in getpass for a console."""
    key = tmp_path / "id_test"
    key.write_text("(fake private key)")
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(
            255, ["ssh-keygen"], stderr="incorrect passphrase supplied"
        ),
    )
    mocker.patch("colab_cli.secrets._can_prompt", return_value=False)
    gp = mocker.patch("getpass.getpass")
    with pytest.raises(typer.Exit) as exc_info:
        ssh_module._pubkey_from_identity(str(key))
    assert exc_info.value.exit_code == 2
    gp.assert_not_called()
    assert "passphrase" in capsys.readouterr().err


@pytest.mark.parametrize("can_prompt", [True, False])
def test_ssh_tells_the_proxy_child_whether_it_may_prompt(mocker, can_prompt):
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    mocker.patch("colab_cli.secrets._can_prompt", return_value=can_prompt)
    call = mocker.patch("subprocess.call", return_value=0)
    session = MagicMock(endpoint="e")
    ssh_module._call_ssh(["ssh", "-o", "X", "root@colab-runtime"], session, False)
    args = call.call_args.args[0]
    env = call.call_args.kwargs["env"]
    assert env[ssh_module._CAN_PROMPT_ENV] == ("1" if can_prompt else "0")
    # No terminal: ssh itself must not wait for a key passphrase either.
    assert ("BatchMode=yes" in args) is (not can_prompt)
    assert args[0] == "ssh" and args[-1] == "root@colab-runtime"


@pytest.mark.parametrize(("flag", "expected"), [("1", True), ("0", False)])
def test_proxy_child_follows_the_parent_flag(monkeypatch, mocker, flag, expected):
    """The child's stdin is the ssh pipe, so the parent's answer wins."""
    mocker.patch("colab_cli.secrets._can_prompt", return_value=not expected)
    monkeypatch.setenv(ssh_module._CAN_PROMPT_ENV, flag)
    assert ssh_module._can_prompt_for_passphrase() is expected
