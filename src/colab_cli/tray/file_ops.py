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

"""What the tray's file manager runs, as CLI argv (docs/09). No Qt.

Every action is one of the CLI's own subcommands (`colab ls --json`,
`upload`, `download`, `mkdir`, `rm`, `ssh -- CMD`, `exec`), so the window
never talks to the runtime itself. Remote paths are contents-API paths
(`content/data`), as `colab ls` takes them.
"""

import json
import os
import posixpath
import shlex
from typing import List, Optional, Tuple

VIAS = ("contents", "ssh")


def remote_join(folder: str, name: str) -> str:
    return posixpath.join(folder, name) if folder else name


def remote_parent(path: str) -> str:
    return posixpath.dirname(path.rstrip("/"))


def ls_argv(session: str, path: str) -> List[str]:
    return ["ls", "-s", session, "--json", path]


def copy_argv(session: str, source: str, target_dir: str, *, upload: bool, is_dir: bool, via: str) -> List[str]:
    """Copy ``source`` into ``target_dir`` on the other side, keeping its name."""
    if upload:
        target = remote_join(target_dir, os.path.basename(source.rstrip("/\\")))
    else:
        target = os.path.join(target_dir, posixpath.basename(source.rstrip("/")))
    argv = ["upload" if upload else "download", "-s", session]
    if is_dir:
        argv.append("-r")
    return argv + ["--via", via, source, target]


def mkdir_argv(session: str, path: str) -> List[str]:
    return ["mkdir", "-s", session, path]


def rm_argv(session: str, path: str, *, is_dir: bool) -> List[str]:
    return ["rm", "-s", session, *(["-r"] if is_dir else []), path]


def command_argv(session: str, cwd: str, command: str, *, via: str) -> Tuple[List[str], Optional[str]]:
    """The command line: run ``command`` in the remote folder ``cwd``.

    Over ssh it is `colab ssh -- sh -c`; otherwise `colab exec` gets Python
    code on stdin, so it works without an SSH key. Returns (argv, stdin).
    """
    folder = "/" + cwd.strip("/")
    if via == "ssh":
        return ["ssh", "-s", session, "--", "sh", "-c", f"cd {shlex.quote(folder)} && {command}"], None
    code = (
        "import subprocess, sys\n"
        f"r = subprocess.run({command!r}, shell=True, cwd={folder!r}, capture_output=True, text=True)\n"
        "sys.stdout.write(r.stdout + r.stderr)\n"
        "print(f'[exit {r.returncode}]')\n"
    )
    return ["exec", "-s", session], code


def parse_listing(text: str) -> List[dict]:
    """The items of `colab ls --json` output; raises ValueError with its error."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            data = json.loads(line)
            if "error" in data:
                raise ValueError(data["error"])
            return data.get("items") or []
    raise ValueError(text.strip().splitlines()[-1] if text.strip() else "no output")


def human_size(size: Optional[int]) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""
