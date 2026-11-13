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

"""Run profiles (docs/09_tray_and_profiles.md): named, saved ways to run something.

A profile lives in ``profiles.toml`` next to sessions.json and says what to
run and on which runtime. Two modes:

* ``job`` (default): the script (or notebook) runs as a background
  `colab job` on a new runtime (or on ``session`` if it is alive); the tray
  follows its progress and, with ``stop_when_done``, stops the runtime after it.
* ``kernel``: the script runs in the kernel with `colab exec` (for code that
  starts a server and returns, or needs the kernel's ``google.colab`` APIs);
  ``vars`` sets Python globals first, and ``forward`` keeps a `colab forward`
  open afterwards, opening ``open`` once ``ready`` answers.

Tokens never go in the file: ``secrets`` names entries of `colab secret`.
"""

import os
import re
import tomllib
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

PROFILES_FILE = "profiles.toml"
_PORT_SPEC = re.compile(r"^\d+(:\d+)?$|^\d+:[^:\s]+:\d+$")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class ProfileError(ValueError):
    """A profiles.toml that cannot be read or a profile that is not valid."""


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: str
    description: Optional[str] = None
    mode: Literal["job", "kernel"] = "job"
    args: List[str] = []
    session: Optional[str] = None
    gpu: Optional[str] = None
    tpu: Optional[str] = None
    high_mem: bool = False
    budget: Optional[float] = None
    timeout: Optional[float] = None
    secrets: List[str] = []
    env: Dict[str, str] = {}
    vars: Dict[str, Any] = {}
    forward: List[str] = []
    ssh_key: Optional[str] = None
    ready: Optional[str] = None
    open: Optional[str] = None
    ready_timeout: float = 900.0
    stop_when_done: bool = True

    @field_validator("gpu", "tpu")
    @classmethod
    def _accelerator(cls, value, info):
        if value is None:
            return None
        from colab_cli.commands.session import GPU_ACCELERATORS, TPU_ACCELERATORS

        table = GPU_ACCELERATORS if info.field_name == "gpu" else TPU_ACCELERATORS
        if value.lower() not in table:
            supported = ", ".join(k.upper() for k in table)
            raise ValueError(f"unsupported {info.field_name} {value!r}; supported: {supported}")
        return value.upper()

    @field_validator("budget")
    @classmethod
    def _positive(cls, value):
        if value is not None and value <= 0:
            raise ValueError("budget must be more than 0 compute units")
        return value

    @field_validator("forward", mode="before")
    @classmethod
    def _ports(cls, value):
        ports = []
        for item in value or []:
            text = str(item)
            if isinstance(item, bool) or not _PORT_SPEC.match(text):
                raise ValueError(f"forward entry {item!r} is not PORT, LOCAL:REMOTE or LOCAL:HOST:REMOTE")
            ports.append(text)
        return ports

    @field_validator("vars")
    @classmethod
    def _names(cls, value):
        for name in value:
            if not name.isidentifier():
                raise ValueError(f"vars name {name!r} is not a Python name")
        return value

    @model_validator(mode="after")
    def _consistent(self):
        if self.gpu and self.tpu:
            raise ValueError("give gpu or tpu, not both")
        if self.mode == "job" and self.vars:
            raise ValueError("vars need mode = \"kernel\" (a job is its own process; use env)")
        if self.mode == "kernel" and self.budget is not None:
            raise ValueError("budget applies to mode = \"job\" only")
        if self.mode == "kernel" and self.args:
            raise ValueError("args apply to mode = \"job\" only (use vars in kernel mode)")
        return self

    @property
    def ready_url(self) -> Optional[str]:
        return self.ready or self.open


def profiles_path(config_path: Optional[str]) -> str:
    """profiles.toml next to the sessions file (``--config``) or in ~/.config/colab-cli."""
    if config_path:
        return os.path.join(os.path.dirname(os.path.abspath(config_path)), PROFILES_FILE)
    return os.path.join(os.path.expanduser("~"), ".config", "colab-cli", PROFILES_FILE)


def _read_raw(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{path}: {e}") from e


def _error_text(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        where = ".".join(str(p) for p in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            msg = "unknown key"
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


def load_profiles(path: str) -> Dict[str, Profile]:
    """All profiles in ``path`` (none when the file does not exist)."""
    profiles = {}
    for name, body in _read_raw(path).items():
        if not isinstance(body, dict):
            raise ProfileError(f"Profile {name!r} in {path}: expected a [{name}] table.")
        try:
            profiles[name] = Profile(**body)
        except ValidationError as e:
            raise ProfileError(f"Profile {name!r} in {path}: {_error_text(e)}") from None
    return profiles


def resolve_script(profile: Profile, path: str) -> str:
    """The script path, ``~`` expanded and relative to the profiles file."""
    script = os.path.expanduser(profile.script)
    if not os.path.isabs(script):
        script = os.path.join(os.path.dirname(os.path.abspath(path)), script)
    return os.path.normpath(script)


# --- writing (plain TOML for the value types a profile uses) ---

def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _string(key)


def _string(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }" if value else "{}"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def dump_toml(data: Dict[str, Dict[str, Any]]) -> str:
    """``{table: {key: value}}`` as TOML, one ``[table]`` per profile."""
    blocks = []
    for name, body in data.items():
        lines = [f"[{_key(name)}]"]
        lines.extend(f"{_key(k)} = {_value(v)}" for k, v in body.items())
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _write(path: str, raw: Dict[str, Any]) -> None:
    from colab_cli.state import make_private, private_opener

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n", opener=private_opener) as f:
        f.write(dump_toml(raw))
    make_private(path)


def save_profile(path: str, name: str, profile: Profile) -> None:
    """Add or replace one profile. The file is rewritten (comments are lost)."""
    raw = _read_raw(path)
    raw[name] = profile.model_dump(exclude_defaults=True)
    _write(path, raw)


def remove_profile(path: str, name: str) -> bool:
    raw = _read_raw(path)
    if name not in raw:
        return False
    del raw[name]
    _write(path, raw)
    return True


def profile_summary(profile: Profile) -> Dict[str, Union[str, List[str], None]]:
    """What `colab profile list` and the tray show for a profile."""
    hardware = profile.gpu or profile.tpu or "CPU"
    return {
        "mode": profile.mode,
        "hardware": hardware,
        "script": profile.script,
        "forward": profile.forward,
        "description": profile.description,
    }
