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

"""The standalone `colab` binary (PyInstaller): no separate Python inside."""

import shlex
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from colab_cli import auto_update
from colab_cli.cli import app
from colab_cli.commands import ssh as ssh_module

runner = CliRunner()


@pytest.fixture
def frozen(mocker):
    mocker.patch.object(sys, "frozen", True, create=True)
    mocker.patch.object(sys, "executable", "/opt/colab/colab")


def test_proxy_command_runs_the_binary_itself(frozen, mocker):
    """`python -m colab_cli.cli` does not exist in a binary: run `colab ssh`."""
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    cmd = ssh_module._proxy_command(SimpleNamespace(name="s1"), None, "interactive")
    argv = shlex.split(cmd)
    assert argv[0] == "/opt/colab/colab"
    assert "-m" not in argv and "colab_cli.cli" not in argv
    assert argv[1:5] == ["ssh", "--proxy-mode", "-s", "s1"]


def test_proxy_command_from_python_is_unchanged(mocker):
    mocker.patch.object(ssh_module.sys, "platform", "linux")
    cmd = ssh_module._proxy_command(SimpleNamespace(name="s1"), None, "interactive")
    assert shlex.split(cmd)[:3] == [sys.executable, "-m", "colab_cli.cli"]


def test_update_hint_for_a_binary_points_to_the_download(frozen):
    hint = auto_update._get_install_command("https://x/google_colab_cli-1.0-py3-none-any.whl")
    assert hint.startswith("download")
    assert auto_update.RELEASES_PAGE in hint


def test_install_flag_refuses_for_a_binary(frozen, mocker):
    mocker.patch("colab_cli.auto_update.platform.system", return_value="Linux")
    mocker.patch("colab_cli.auto_update.check_for_updates")
    run = mocker.patch("colab_cli.auto_update.subprocess.run")
    mocker.patch("colab_cli.auto_update.get_app_version", return_value="1.0")
    settings = SimpleNamespace(latest_version="2.0", latest_wheel_url="https://x/w.whl")
    mocker.patch("colab_cli.state.SettingsStore.load", return_value=settings)
    result = runner.invoke(app, ["update", "--install"])
    assert result.exit_code == 1
    run.assert_not_called()
    assert "binary" in result.output
