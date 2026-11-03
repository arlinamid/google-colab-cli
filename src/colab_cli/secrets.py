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

"""Reading secrets (tokens) locally, so they never sit on the command line.

A secret spec is ``NAME[=SOURCE]``. SOURCE says where the value comes from:

* ``env`` (default) or ``env:VAR``: a local environment variable.
* ``prompt``: typed at a hidden prompt on the terminal.
* ``hf-cli``: ``hf auth token`` (falls back to the stored Hugging Face token).
* ``gh-cli``: ``gh auth token``.

A literal value (``NAME=hf_abc``) is refused: it would end up in the shell
history and in the process list. Every resolved value is registered with the
history logger, which writes ``***`` in its place.
"""

import getpass
import os
import re
import shutil
import subprocess
import sys
from typing import List, Optional

import typer

NAME_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLAIN_SOURCES = ("env", "prompt", "hf-cli", "gh-cli")
_CLI_TIMEOUT_SEC = 20


def _fail(message: str) -> None:
    typer.echo(f"[colab] {message}", err=True)
    raise typer.Exit(code=2)


def parse_source(source: str) -> str:
    """Validates a SOURCE (the part after ``=``) and returns it unchanged.

    Raises:
      typer.Exit: If it is not a known source (exit code 2). The message never
        repeats the value, because a mistyped source may be a pasted token.
    """
    if source in _PLAIN_SOURCES:
        return source
    if source.startswith("env:") and NAME_REGEX.fullmatch(source[4:]):
        return source
    _fail(
        "Secret values are not accepted on the command line. Use env:VAR "
        "(a local environment variable), prompt, hf-cli or gh-cli."
    )


def parse_spec(spec: str) -> tuple[str, str]:
    """Splits ``NAME[=SOURCE]`` into ``(NAME, SOURCE)``, ``env`` -> ``env:NAME``.

    Raises:
      typer.Exit: On an invalid name or source (exit code 2).
    """
    name, sep, source = spec.partition("=")
    if not NAME_REGEX.fullmatch(name):
        _fail(f"Invalid secret name {name!r}. Expected an environment variable name.")
    source = parse_source(source) if sep else "env"
    if source == "env":
        source = f"env:{name}"
    return name, source


def _token_from_cli(argv: List[str]) -> Optional[str]:
    # shutil.which finds Windows launchers (hf.exe, gh.cmd) that a bare
    # CreateProcess lookup would miss. stdin=DEVNULL: a CLI that wants to
    # prompt must fail, not wait on our stdin.
    exe = shutil.which(argv[0]) or argv[0]
    try:
        res = subprocess.run(
            [exe, *argv[1:]],
            check=True,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_CLI_TIMEOUT_SEC,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None
    return res.stdout.strip() or None


def _hf_token_file() -> Optional[str]:
    path = os.environ.get("HF_TOKEN_PATH") or os.path.join(
        os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"),
        "token",
    )
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def read_source(name: str, source: str) -> Optional[str]:
    """Reads one secret from SOURCE; None when that source has no value.

    Raises:
      typer.Exit: If ``prompt`` has no terminal to ask on (exit code 2).
    """
    if source == "env":
        source = f"env:{name}"
    if source.startswith("env:"):
        return os.environ.get(source[4:]) or None
    if source == "prompt":
        try:
            return getpass.getpass(f"[colab] {name}: ", stream=sys.stderr) or None
        except (EOFError, OSError):
            _fail(f"No terminal to ask for {name}. Use env:VAR instead.")
    if source == "hf-cli":
        return _token_from_cli(["hf", "auth", "token"]) or _hf_token_file()
    if source == "gh-cli":
        return _token_from_cli(["gh", "auth", "token"])
    raise ValueError(f"unknown secret source {source!r}")


def resolve_secrets(specs: List[str], history) -> dict[str, str]:
    """Resolves every ``NAME[=SOURCE]`` spec and registers each value for redaction.

    Raises:
      typer.Exit: If a spec is invalid or its source has no value (exit code 2).
    """
    resolved = {}
    for spec in specs or []:
        name, source = parse_spec(spec)
        value = read_source(name, source)
        if value is None:
            _fail(f"No value for secret {name} from {source}.")
        history.add_redaction(value)
        resolved[name] = value
    return resolved


def resolve_first(name: str, sources: List[str], history) -> Optional[str]:
    """Returns the first value found among ``sources``, registered for redaction."""
    for source in sources:
        value = read_source(name, source)
        if value is not None:
            history.add_redaction(value)
            return value
    return None


def build_secret_prelude(values: dict[str, str]) -> str:
    """Python source that sets ``values`` in the kernel's ``os.environ``."""
    if not values:
        return ""
    lines = ["import os"]
    lines.extend(f"os.environ[{k!r}] = {v!r}" for k, v in values.items())
    return "\n".join(lines) + "\n"
