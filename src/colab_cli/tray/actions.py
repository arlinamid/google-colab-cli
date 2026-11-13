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


def log_argv(card: Dict[str, Any]) -> List[str]:
    if card["job_id"]:
        return ["job", "tail", "-s", card["name"], card["job_id"], "--follow"]
    return ["log", "-s", card["name"]]


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


def terminal_command(argv: List[str], system: Optional[str] = None,
                     which: Callable[[str], Optional[str]] = shutil.which) -> Tuple[List[str], Dict[str, Any]]:
    """How to run ``argv`` in a new terminal window: (command, Popen kwargs)."""
    system = system or sys.platform
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
