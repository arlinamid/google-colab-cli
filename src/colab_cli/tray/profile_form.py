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

"""The profile editor's form, without Qt: profile <-> text fields, and help.

Every field is a string in the form (lists as one line, env as KEY=VALUE
lines, vars as a JSON object) so the window stays a plain form; converting
back goes through the Profile model, whose errors name the field.
"""

import json
import shlex
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from colab_cli.profiles import Profile

# (field, kind, modes). kind: line, choice, check, text, file
FIELDS: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("description", "line", ("job", "kernel")),
    ("mode", "choice", ("job", "kernel")),
    ("script", "file", ("job", "kernel")),
    ("args", "line", ("job",)),
    ("session", "line", ("job", "kernel")),
    ("hardware", "choice", ("job", "kernel")),
    ("high_mem", "check", ("job", "kernel")),
    ("budget", "line", ("job",)),
    ("timeout", "line", ("kernel",)),
    ("secrets", "line", ("job", "kernel")),
    ("env", "text", ("job", "kernel")),
    ("vars", "text", ("kernel",)),
    ("forward", "line", ("kernel",)),
    ("ssh_key", "file", ("kernel",)),
    ("open", "line", ("kernel",)),
    ("ready", "line", ("kernel",)),
    ("stop_when_done", "check", ("job", "kernel")),
]


def hardware_choices() -> List[str]:
    from colab_cli.commands.session import GPU_ACCELERATORS, TPU_ACCELERATORS

    return ["CPU", *(k.upper() for k in GPU_ACCELERATORS), *(f"TPU {k.upper()}" for k in TPU_ACCELERATORS)]


def to_form(profile: Optional[Profile]) -> Dict[str, Any]:
    """A profile (or a new one) as form values."""
    p = profile or Profile(script="")
    hardware = p.gpu or (f"TPU {p.tpu}" if p.tpu else "CPU")
    return {
        "description": p.description or "",
        "mode": p.mode,
        "script": p.script,
        "args": shlex.join(p.args),
        "session": p.session or "",
        "hardware": hardware,
        "high_mem": p.high_mem,
        "budget": "" if p.budget is None else f"{p.budget:g}",
        "timeout": "" if p.timeout is None else f"{p.timeout:g}",
        "secrets": ", ".join(p.secrets),
        "env": "\n".join(f"{k}={v}" for k, v in p.env.items()),
        "vars": _vars_text(p.vars),
        "forward": ", ".join(p.forward),
        "ssh_key": p.ssh_key or "",
        "open": p.open or "",
        "ready": p.ready or "",
        "stop_when_done": p.stop_when_done,
    }


def _vars_text(variables: Dict[str, Any]) -> str:
    """One line when short, indented when long."""
    if not variables:
        return ""
    one = json.dumps(variables, ensure_ascii=False)
    return one if len(one) <= 70 else json.dumps(variables, indent=2, ensure_ascii=False)


class FormError(ValueError):
    """A form value that cannot be a profile; ``field`` names it."""

    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def _number(field: str, text: str) -> Optional[float]:
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise FormError(field, f"not a number: {text!r}") from None


def from_form(values: Dict[str, Any]) -> Profile:
    """Form values to a Profile; raises FormError naming the field."""
    mode = values["mode"]
    try:
        args = shlex.split(values.get("args") or "", posix=True) if mode == "job" else []
    except ValueError as e:
        raise FormError("args", str(e)) from None
    env = {}
    for line in (values.get("env") or "").splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise FormError("env", f"expected KEY=VALUE, got {line.strip()!r}")
        env[key.strip()] = value
    variables = {}
    if mode == "kernel" and (values.get("vars") or "").strip():
        try:
            variables = json.loads(values["vars"])
        except ValueError as e:
            raise FormError("vars", f"not valid JSON: {e}") from None
        if not isinstance(variables, dict):
            raise FormError("vars", "must be a JSON object, e.g. {\"CONFIG\": {\"a\": 1}}")
    hardware = values.get("hardware") or "CPU"
    gpu = tpu = None
    if hardware.startswith("TPU "):
        tpu = hardware[4:]
    elif hardware != "CPU":
        gpu = hardware
    kernel = mode == "kernel"
    fields = {
        "script": (values.get("script") or "").strip(),
        "description": (values.get("description") or "").strip() or None,
        "mode": mode,
        "args": args,
        "session": (values.get("session") or "").strip() or None,
        "gpu": gpu,
        "tpu": tpu,
        "high_mem": bool(values.get("high_mem")),
        "budget": None if kernel else _number("budget", values.get("budget") or ""),
        "timeout": _number("timeout", values.get("timeout") or "") if kernel else None,
        "secrets": [s.strip() for s in (values.get("secrets") or "").split(",") if s.strip()],
        "env": env,
        "vars": variables,
        "forward": [f.strip() for f in (values.get("forward") or "").split(",") if f.strip()] if kernel else [],
        "ssh_key": ((values.get("ssh_key") or "").strip() or None) if kernel else None,
        "open": ((values.get("open") or "").strip() or None) if kernel else None,
        "ready": ((values.get("ready") or "").strip() or None) if kernel else None,
        "stop_when_done": bool(values.get("stop_when_done")),
    }
    if not fields["script"]:
        raise FormError("script", "choose the script or notebook to run")
    try:
        return Profile(**fields)
    except ValidationError as e:
        err = e.errors()[0]
        field = str(err["loc"][0]) if err["loc"] else "script"
        if field in ("gpu", "tpu"):
            field = "hardware"
        raise FormError(field, err["msg"].removeprefix("Value error, ")) from None


def valid_name(name: str) -> bool:
    return bool(name) and all(c.isalnum() or c in "-_." for c in name)
