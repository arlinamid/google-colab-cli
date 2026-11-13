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

"""What the panel's buttons do, without Qt: texts, CLI commands, terminals.

Every action runs one of the CLI's own subcommands (`colab_argv()`), so the
tray changes nothing by itself.
"""

import os
import shlex
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from colab_cli.tray.i18n import fmt_age, fmt_duration, fmt_number, hardware_label, t


def card_lines(card: Dict[str, Any], lang: str) -> Dict[str, Optional[str]]:
    """The texts of one card: subtitle, metric, detail line under the bar."""
    state = t(lang, f"state.{card['state']}")
    subtitle = f"{hardware_label(card['hardware'])} · {state}"
    if not card["local"]:
        subtitle += f" · {t(lang, 'orphan')}"
    metric = None
    if card["metric"]:
        m = card["metric"]
        arrow = {"down": " ↓", "up": " ↑"}.get(m["trend"], "")
        value = m["value"]
        shown = fmt_number(value, lang, 2) if isinstance(value, float) else str(value)
        metric = f"{m['name']} {shown}{arrow}"
    if card["state"] == "starting":
        parts = [t(lang, f"phase.{card.get('phase') or 'launching'}")]
        if card.get("elapsed_s") is not None:
            parts.append(t(lang, "elapsed", time=fmt_duration(card["elapsed_s"], lang)))
        if card.get("setup_line"):
            parts.append(f"› {card['setup_line']}")
        return {"subtitle": subtitle, "metric": metric, "detail": " · ".join(parts)}
    parts = []
    if card["epoch"]:
        parts.append(t(lang, "epoch", cur=card["epoch"][0], total=card["epoch"][1]))
    if card["cell"]:
        cur, total, label = card["cell"]
        parts.append(t(lang, "cell", cur=cur, total=total, label=label or ""))
    running = card["state"] in ("running", "stalled")
    if running and card["eta_s"] is not None:
        parts.append(t(lang, "eta", time=fmt_duration(card["eta_s"], lang)))
    if running and card["percent"] is None:
        age = card["last_output_age_s"]
        parts.append(t(lang, "last_output", age=fmt_age(age, lang)) if age is not None else t(lang, "no_output_yet"))
    if card["diagnosis"]:
        parts.append(card["diagnosis"])
    if card.get("forwards"):
        ports = [
            str(f["local_port"]) if f.get("listening") else t(lang, "not_answering", port=f["local_port"])
            for f in card["forwards"]
        ]
        parts.append(t(lang, "forwarding", ports=", ".join(ports)))
    if not card["local"]:
        parts.append(t(lang, "orphan_hint"))
    return {"subtitle": subtitle, "metric": metric, "detail": " · ".join(parts) or None}


def stop_options(card: Dict[str, Any], has_service: bool, lang: str) -> List[Tuple[str, str]]:
    """The choices of the stop dialog; each says plainly what stops."""
    options = []
    if card["can_stop_job"] and card["job_id"]:
        options.append(("job", t(lang, "stop.job", job=card["job_id"])))
    runtime = t(lang, "stop.runtime", name=card["name"], hw=hardware_label(card["hardware"]))
    if has_service:
        runtime += " " + t(lang, "stop.service")
    options.append(("runtime", runtime))
    return options


def stop_argv(card: Dict[str, Any], choice: str) -> List[str]:
    if choice == "job":
        return ["job", "stop", "-s", card["name"], card["job_id"]]
    return ["stop", "-s", card["name"]]


def log_argv(card: Dict[str, Any], follow: bool = False) -> List[str]:
    """The job's last output (``--follow``: and new lines), else the session history."""
    if card["job_id"]:
        return ["job", "tail", "-s", card["name"], card["job_id"], "-n", "200", *(["--follow"] if follow else [])]
    return ["log", "-s", card["name"], "-n", "200"]


def ssh_argv(card: Dict[str, Any]) -> List[str]:
    return ["ssh", "-s", card["name"]]


def profile_confirm(name: str, profile, rate: Optional[float], lang: str) -> Tuple[str, str]:
    """Title and body of the dialog before a profile starts (it may cost CU)."""
    hw = hardware_label(profile.gpu or profile.tpu or "CPU")
    body = [t(lang, "start.body", mode=t(lang, f"mode.{profile.mode}"), hw=hw, script=profile.script)]
    if profile.description:
        body.insert(0, profile.description)
    if profile.session:
        body.append(t(lang, "start.reuse", session=profile.session))
    if rate is not None:
        body.append(t(lang, "start.rate", rate=fmt_number(rate, lang)))
    return t(lang, "start.title", name=name), "\n".join(body)


def notification(event: Dict[str, Any], lang: str) -> str:
    text = t(lang, f"notify.{event['kind']}", session=event.get("session") or "")
    if event.get("code"):
        text += f" ({event['code']})"
    return text


def held(argv: List[str], lang: str) -> List[str]:
    """``argv`` wrapped so its terminal window waits for Enter after it exits."""
    return [sys.executable, "-m", "colab_cli.tray.hold", "--lang", lang, "--", *argv]


# Terminals offered in the settings ("{cmd}" is the command to run). Any
# other command can be typed there; empty is automatic.
TERMINAL_PRESETS = {
    "win32": [("Windows Terminal", "wt -w new {cmd}")],
    "darwin": [],
    "linux": [
        ("GNOME Terminal", "gnome-terminal -- {cmd}"), ("Konsole", "konsole -e {cmd}"),
        ("Xfce Terminal", "xfce4-terminal -x {cmd}"), ("kitty", "kitty {cmd}"),
        ("Alacritty", "alacritty -e {cmd}"), ("xterm", "xterm -e {cmd}"),
    ],
}


def terminal_presets(system: Optional[str] = None) -> List[Tuple[str, str]]:
    system = system or sys.platform
    return list(TERMINAL_PRESETS.get("linux" if system.startswith("linux") else system, []))


def _split_template(template: str, system: str) -> List[str]:
    if system != "win32":
        return shlex.split(template)
    # Windows paths have backslashes: split without POSIX escapes, drop the quotes.
    parts = shlex.split(template, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]


def terminal_command(argv: List[str], system: Optional[str] = None,
                     which: Callable[[str], Optional[str]] = shutil.which,
                     custom: Optional[str] = None) -> Tuple[List[str], Dict[str, Any]]:
    """How to run ``argv`` in a new terminal window: (command, Popen kwargs).

    ``custom`` is the terminal chosen in the settings: a command where
    ``{cmd}`` stands for ``argv`` (a word of its own: the arguments; inside a
    word: one quoted command line); without ``{cmd}``, ``argv`` goes last.
    """
    system = system or sys.platform
    if custom and custom.strip():
        line = subprocess.list2cmdline(argv) if system == "win32" else shlex.join(argv)
        cmd: List[str] = []
        placed = False
        for part in _split_template(custom, system):
            if part == "{cmd}":
                cmd += argv
                placed = True
            elif "{cmd}" in part:
                cmd.append(part.replace("{cmd}", line))
                placed = True
            else:
                cmd.append(part)
        if not placed:
            cmd += argv
        if system == "win32":
            return cmd, {"creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)}
        return cmd, {"start_new_session": True}
    if system == "win32":
        return list(argv), {"creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)}
    line = shlex.join(argv)
    if system == "darwin":
        script = 'tell application "Terminal" to do script ' + _applescript_string(line)
        return ["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'], {}
    for term, prefix in (
        ("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
        ("xfce4-terminal", ["-x"]), ("kitty", []), ("alacritty", ["-e"]), ("xterm", ["-e"]),
    ):
        path = which(term)
        if path:
            return [path, *prefix, *argv], {"start_new_session": True}
    raise FileNotFoundError("no terminal emulator found (x-terminal-emulator, gnome-terminal, konsole, xterm...)")


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def background_kwargs(log_path: Optional[str] = None) -> Dict[str, Any]:
    """Popen kwargs for a CLI child with no window; output to ``log_path``."""
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        out = open(log_path, "ab")
        kw.update(stdout=out, stderr=subprocess.STDOUT)
    else:
        kw.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    return kw


def status_text(icon: Dict[str, Any], cards: List[Dict[str, Any]], lang: str) -> str:
    """The icon's state in words (the tooltip): level, runtimes, reasons."""
    parts = [t(lang, f"level.{icon['level']}")]
    if cards:
        items = []
        for c in cards:
            item = f"{c['name']} {t(lang, 'state.' + c['state']).lower()}"
            if c["percent"] is not None and c["state"] in ("running", "stalled"):
                item += f" {c['percent']:g}%"
            items.append(item)
        parts.append(t(lang, "summary.n", n=len(cards)) + ": " + ", ".join(items))
    for r in icon.get("reasons", []):
        params = {k: (", ".join(v) if isinstance(v, list) else v) for k, v in r.items() if k != "code"}
        parts.append(t(lang, f"reason.{r['code']}", **params))
    return f"{t(lang, 'tooltip.prefix')} – " + " · ".join(parts)
