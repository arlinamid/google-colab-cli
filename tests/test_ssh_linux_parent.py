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

"""Linux: ssh, and the standalone binary's CLI process, die with their parent.

A forced kill (SIGKILL) of colab, or of the PyInstaller launcher of the
binary, used to leave ssh and its ProxyCommand running: SIGHUP only reaches
them when the terminal closes. PR_SET_PDEATHSIG makes the kernel send
SIGTERM to a child when its parent dies.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from unittest.mock import MagicMock

import pytest

from colab_cli.commands import ssh as ssh_module


@pytest.fixture
def libc(mocker):
    lib = MagicMock()
    lib.prctl.return_value = 0
    mocker.patch("ctypes.CDLL", return_value=lib)
    return lib


def test_parent_death_signal_on_linux(mocker, libc):
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    assert ssh_module._set_parent_death_signal(signal.SIGTERM) is True
    libc.prctl.assert_called_once_with(1, int(signal.SIGTERM), 0, 0, 0)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_parent_death_signal_elsewhere_does_nothing(mocker, libc, platform):
    mocker.patch.object(ssh_module.sys, "platform", platform)
    assert ssh_module._set_parent_death_signal(signal.SIGTERM) is False
    libc.prctl.assert_not_called()


@pytest.mark.parametrize("platform, expected", [("linux", True), ("win32", False), ("darwin", False)])
def test_ssh_gets_the_preexec_only_on_linux(mocker, monkeypatch, platform, expected):
    mocker.patch.object(ssh_module.sys, "platform", platform)
    mocker.patch.object(ssh_module, "_kill_children_with_this_process")
    mocker.patch.object(ssh_module.colab_secrets, "_can_prompt", return_value=True)
    call = mocker.patch.object(ssh_module.subprocess, "call", return_value=0)

    ssh_module._call_ssh(["ssh", "host"], MagicMock(), keep_alive=False)

    kwargs = call.call_args.kwargs
    assert (kwargs.get("preexec_fn") is ssh_module._ssh_preexec) is expected


def test_binary_child_ties_itself_to_its_launcher(mocker, monkeypatch):
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    monkeypatch.setattr(ssh_module.sys, "frozen", True, raising=False)
    mocker.patch.object(ssh_module.sys, "executable", "/opt/colab")
    mocker.patch.object(ssh_module.os, "getppid", return_value=77)
    mocker.patch.object(ssh_module.os, "readlink", return_value="/opt/colab")
    pdeath = mocker.patch.object(ssh_module, "_set_parent_death_signal", return_value=True)

    assert ssh_module._tie_to_launcher_linux() is True
    pdeath.assert_called_once_with(signal.SIGTERM)


def test_other_parents_are_left_alone(mocker, monkeypatch):
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    monkeypatch.setattr(ssh_module.sys, "frozen", True, raising=False)
    mocker.patch.object(ssh_module.sys, "executable", "/opt/colab")
    mocker.patch.object(ssh_module.os, "getppid", return_value=77)
    mocker.patch.object(ssh_module.os, "readlink", return_value="/usr/bin/bash")
    pdeath = mocker.patch.object(ssh_module, "_set_parent_death_signal")

    assert ssh_module._tie_to_launcher_linux() is False
    pdeath.assert_not_called()


def test_python_install_is_left_alone(mocker, monkeypatch):
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    monkeypatch.delattr(ssh_module.sys, "frozen", raising=False)
    pdeath = mocker.patch.object(ssh_module, "_set_parent_death_signal")
    assert ssh_module._tie_to_launcher_linux() is False
    pdeath.assert_not_called()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_real_grandchild_dies_when_its_parent_is_killed(tmp_path):
    """A middle process starts `sleep` with _ssh_preexec; SIGKILL the middle."""
    pidfile = tmp_path / "pid"
    middle = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import subprocess, time
        from colab_cli.commands.ssh import _ssh_preexec
        p = subprocess.Popen(["sleep", "60"], preexec_fn=_ssh_preexec)
        open({str(pidfile)!r}, "w").write(str(p.pid))
        time.sleep(60)
    """)])
    deadline = time.time() + 20
    while not pidfile.exists() or not pidfile.read_text():
        assert time.time() < deadline, "middle process did not start sleep"
        time.sleep(0.05)
    grandchild = int(pidfile.read_text())
    middle.kill()
    middle.wait()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        # A zombie re-parented to init counts as gone once reaped.
        try:
            with open(f"/proc/{grandchild}/stat") as f:
                if f.read().split()[2] == "Z":
                    return
        except FileNotFoundError:
            return
        time.sleep(0.1)
    os.kill(grandchild, signal.SIGKILL)
    pytest.fail("sleep outlived its killed parent")
