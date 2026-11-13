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

"""Start `colab tray` when the user signs in (docs/09_tray_and_profiles.md).

Per user, no administrator rights, nothing installed system-wide:

* Windows: a value in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run,
  run with pythonw.exe so no console window opens at sign-in;
* macOS: a LaunchAgent in ~/Library/LaunchAgents with RunAtLoad;
* Linux: an XDG autostart entry, ~/.config/autostart/colab-cli-tray.desktop.

The operating system holds the setting (there is no copy in settings.json),
so `status()` tells what really happens at the next sign-in. No Qt here:
`colab tray --autostart` works without the [tray] extra's GUI.
"""

import os
import plistlib
import subprocess
import sys
from typing import List, Optional

WINDOWS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_VALUE = "Colab CLI tray"
MAC_LABEL = "com.github.arlinamid.colab-cli.tray"
DESKTOP_FILE = "colab-cli-tray.desktop"


class _WindowsRun:
    """HKCU\\...\\Run through winreg (Windows only)."""

    def get(self, name: str) -> Optional[str]:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_KEY) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
            return True
        except FileNotFoundError:
            return False


def _system(system: Optional[str]) -> str:
    return system or sys.platform


def startup_command(argv: List[str], system: Optional[str] = None) -> List[str]:
    """``argv`` (the CLI's own command plus ``tray``) as run at sign-in.

    On Windows python.exe becomes pythonw.exe next to it, when there is one:
    python.exe opens a console window that would stay open all session.
    """
    if _system(system) == "win32" and argv:
        exe = argv[0]
        head, name = os.path.split(exe)
        if name.lower() == "python.exe":
            windowless = os.path.join(head, "pythonw.exe")
            if os.path.isfile(windowless):
                return [windowless, *argv[1:]]
    return list(argv)


def _desktop_quote(arg: str) -> str:
    """One Exec= argument (Desktop Entry Specification, "The Exec key")."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'


def _home(home: Optional[str]) -> str:
    return home or os.path.expanduser("~")


def _mac_path(home: Optional[str]) -> str:
    return os.path.join(_home(home), "Library", "LaunchAgents", f"{MAC_LABEL}.plist")


def _desktop_path(home: Optional[str]) -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(home), ".config")
    return os.path.join(config, "autostart", DESKTOP_FILE)


def status(system: Optional[str] = None, home: Optional[str] = None, run_key=None) -> Optional[str]:
    """The command that starts the tray at sign-in, or None when it does not."""
    system = _system(system)
    if system == "win32":
        return (run_key or _WindowsRun()).get(WINDOWS_VALUE)
    if system == "darwin":
        try:
            with open(_mac_path(home), "rb") as f:
                return subprocess.list2cmdline(plistlib.load(f).get("ProgramArguments") or [])
        except (OSError, plistlib.InvalidFileException):
            return None
    try:
        with open(_desktop_path(home), encoding="utf-8") as f:
            for line in f:
                if line.startswith("Exec="):
                    return line[len("Exec="):].strip()
    except OSError:
        pass
    return None


def enable(argv: List[str], system: Optional[str] = None, home: Optional[str] = None, run_key=None) -> str:
    """Start ``argv`` at each sign-in; returns where that is kept."""
    system = _system(system)
    cmd = startup_command(argv, system)
    if system == "win32":
        (run_key or _WindowsRun()).set(WINDOWS_VALUE, subprocess.list2cmdline(cmd))
        return f"HKCU\\{WINDOWS_KEY}\\{WINDOWS_VALUE}"
    if system == "darwin":
        path = _mac_path(home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump({"Label": MAC_LABEL, "ProgramArguments": cmd, "RunAtLoad": True,
                           "ProcessType": "Interactive"}, f)
        return path
    path = _desktop_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Colab CLI tray\n"
            "Comment=Colab runtimes, job progress and compute units in the system tray\n"
            f"Exec={' '.join(_desktop_quote(a) for a in cmd)}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
    return path


def disable(system: Optional[str] = None, home: Optional[str] = None, run_key=None) -> bool:
    """Stop starting the tray at sign-in; False when it was not set up."""
    system = _system(system)
    if system == "win32":
        return (run_key or _WindowsRun()).delete(WINDOWS_VALUE)
    path = _mac_path(home) if system == "darwin" else _desktop_path(home)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
