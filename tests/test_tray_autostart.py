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

"""Starting the tray when the user signs in (docs/09): Windows, macOS, Linux."""

import plistlib

from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import tray as tray_cmd
from colab_cli.tray import autostart as au

runner = CliRunner()


class FakeRun:
    """HKCU\\...\\Run as a dict."""

    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        return self.values.pop(name, None) is not None


def test_windows_starts_without_a_console_window(tmp_path):
    scripts = tmp_path / "My Venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    (scripts / "pythonw.exe").write_text("")
    argv = [str(scripts / "python.exe"), "-m", "colab_cli", "--auth=oauth2", "tray"]

    cmd = au.startup_command(argv, system="win32")

    assert cmd == [str(scripts / "pythonw.exe"), "-m", "colab_cli", "--auth=oauth2", "tray"]
    # The standalone binary has no windowless twin: it is used as it is.
    assert au.startup_command(["C:/x/colab.exe", "tray"], system="win32") == ["C:/x/colab.exe", "tray"]
    assert au.startup_command(argv, system="linux") == argv


def test_windows_run_key(tmp_path):
    run = FakeRun()
    argv = ["C:\\Program Files\\py\\python.exe", "-m", "colab_cli", "tray"]
    assert au.status(system="win32", run_key=run) is None

    where = au.enable(argv, system="win32", run_key=run)

    assert "CurrentVersion\\Run" in where
    assert run.values[au.WINDOWS_VALUE] == '"C:\\Program Files\\py\\python.exe" -m colab_cli tray'
    assert au.status(system="win32", run_key=run) == run.values[au.WINDOWS_VALUE]
    assert au.disable(system="win32", run_key=run) is True
    assert au.status(system="win32", run_key=run) is None
    assert au.disable(system="win32", run_key=run) is False


def test_macos_launch_agent(tmp_path):
    argv = ["/Users/a/.local/bin/colab", "tray"]

    where = au.enable(argv, system="darwin", home=str(tmp_path))

    with open(where, "rb") as f:
        plist = plistlib.load(f)
    assert where.startswith(str(tmp_path / "Library" / "LaunchAgents"))
    assert plist["ProgramArguments"] == argv and plist["RunAtLoad"] is True
    assert plist["Label"] == au.MAC_LABEL
    assert au.status(system="darwin", home=str(tmp_path)) == "/Users/a/.local/bin/colab tray"
    assert au.disable(system="darwin", home=str(tmp_path)) is True
    assert au.status(system="darwin", home=str(tmp_path)) is None


def test_linux_desktop_entry(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    argv = ["/home/a/my tools/python", "-m", "colab_cli", "--config", "/home/a/x.json", "tray"]

    where = au.enable(argv, system="linux", home=str(tmp_path))

    assert where == str(tmp_path / ".config" / "autostart" / "colab-cli-tray.desktop")
    text = (tmp_path / ".config" / "autostart" / "colab-cli-tray.desktop").read_text(encoding="utf-8")
    assert "[Desktop Entry]" in text and "Type=Application" in text
    assert 'Exec="/home/a/my tools/python" -m colab_cli --config /home/a/x.json tray' in text
    assert au.status(system="linux", home=str(tmp_path)).startswith('"/home/a/my tools/python"')
    assert au.disable(system="linux", home=str(tmp_path)) is True and au.status(system="linux", home=str(tmp_path)) is None

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert au.enable(argv, system="linux", home=str(tmp_path)).startswith(str(tmp_path / "xdg" / "autostart"))


def test_cli_turns_autostart_on_and_off(mocker, mock_common_state):
    mocker.patch.object(tray_cmd, "colab_argv", return_value=["colab", "--auth=oauth2"])
    enable = mocker.patch.object(au, "enable", return_value="HKCU\\...\\Run")
    disable = mocker.patch.object(au, "disable", return_value=True)
    status = mocker.patch.object(au, "status", return_value=None)
    gui = mocker.patch.object(tray_cmd, "_load_gui")

    on = runner.invoke(app, ["tray", "--autostart", "on"])
    assert on.exit_code == 0, on.output
    enable.assert_called_once_with(["colab", "--auth=oauth2", "tray"])
    assert "HKCU" in on.output

    off = runner.invoke(app, ["tray", "--autostart", "off"])
    assert off.exit_code == 0 and disable.called

    shown = runner.invoke(app, ["tray", "--autostart", "status"])
    assert shown.exit_code == 0 and "off" in shown.output
    status.return_value = "colab tray"
    assert "on" in runner.invoke(app, ["tray", "--autostart", "status"]).output
    gui.assert_not_called()  # no GUI needed

    bad = runner.invoke(app, ["tray", "--autostart", "maybe"])
    assert bad.exit_code == 2
