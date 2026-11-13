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

"""Port forwards to runtimes that are open on this computer (docs/09).

`colab forward` and `colab ssh -L` run ``ssh -o ProxyCommand=... colab ...
ssh --proxy-mode -s NAME ... -L LOCAL:HOST:REMOTE``. The tray finds those ssh
processes (whichever tool or CLI version started them) and checks that the
local port listens, so a runtime behind a forward shows as a service with an
Open button. Only local process lists and a TCP connect to 127.0.0.1; nothing
is sent to the runtime.
"""

import json
import os
import re
import socket
import subprocess
import sys
from typing import Callable, Dict, List, Optional

_SESSION = re.compile(r'--proxy-mode\s+-s\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))')
_CONFIG = re.compile(r'--config\s+(?:"([^"]+)"|\'([^\']+)\'|(\S+))')
_FORWARD = re.compile(r'(?:^|\s)-L\s*["\']?(?:[\w.\[\]]+:)?(\d+):([^:\s"\']+):(\d+)')


def _first(m) -> Optional[str]:
    if not m:
        return None
    value = next(g for g in m.groups() if g is not None)
    return value.strip("\"'\\")


def parse_ssh_command(command: str) -> Optional[Dict]:
    """The session, config and forwards of one ssh command line, or None.

    Accepts a Windows command line (the ProxyCommand quoted, inner quotes
    escaped as \\") or a POSIX one. Only ssh processes that use the CLI as
    their ProxyCommand (``--proxy-mode``) and forward ports count.
    """
    text = command.replace('\\"', '"')
    if "ProxyCommand" not in text or "--proxy-mode" not in text:
        return None
    session = _first(_SESSION.search(text))
    if not session:
        return None
    head = text[: text.find("--proxy-mode")]
    config = _first(_CONFIG.search(head))
    ports = [
        {"local_port": int(local), "remote": f"{host}:{remote}"}
        for local, host, remote in _FORWARD.findall(text)
    ]
    if not ports:
        return None
    return {"session": session, "config": config, "ports": ports}


def _windows_commands() -> List[str]:
    script = (
        "$p = @(Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
        "Select-Object -ExpandProperty CommandLine); ConvertTo-Json -Compress -InputObject $p"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    data = json.loads(out) if out else []
    return [c for c in (data if isinstance(data, list) else [data]) if c]


def _proc_commands() -> List[str]:
    commands = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == "ssh":
            commands.append(" ".join(f'"{a}"' if " " in a else a for a in argv))
    return commands


def _ps_commands() -> List[str]:
    out = subprocess.run(
        ["ps", "-axww", "-o", "command="], capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
    ).stdout
    return [line for line in out.splitlines() if "--proxy-mode" in line]


def list_ssh_commands(system: Optional[str] = None) -> List[str]:
    """Command lines of the ssh processes on this computer ([] on any error)."""
    system = system or sys.platform
    try:
        if system == "win32":
            return _windows_commands()
        if system.startswith("linux") and os.path.isdir("/proc"):
            return _proc_commands()
        return _ps_commands()
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def port_listening(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_forwards(
    config_path: Optional[str] = None,
    lister: Callable[[], List[str]] = list_ssh_commands,
    listening: Callable[[int], bool] = port_listening,
) -> Dict[str, List[Dict]]:
    """Forwards open on this computer, by session name.

    Only forwards of the same sessions file count: one started with
    ``--config X`` belongs to X, one without to the default.
    """
    want = os.path.abspath(config_path) if config_path else None
    found: Dict[str, List[Dict]] = {}
    for command in lister():
        info = parse_ssh_command(command)
        if not info:
            continue
        have = os.path.abspath(info["config"]) if info["config"] else None
        if have != want:
            continue
        ports = found.setdefault(info["session"], [])
        for p in info["ports"]:
            if all(q["local_port"] != p["local_port"] for q in ports):
                ports.append({**p, "listening": listening(p["local_port"])})
    return found
