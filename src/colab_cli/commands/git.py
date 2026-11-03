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

"""``colab clone``: clone a git repository onto the runtime."""

import base64
import re
from typing import Optional
from urllib.parse import urlparse

import typer
from typing_extensions import Annotated

from colab_cli.commands.automation import exit_if_failed, run_automation
from colab_cli.secrets import parse_source, read_source, resolve_first

_SHORTHAND = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_HOST = "github.com"
# Used when --token is not given; only ever sent to github.com.
_AUTO_TOKEN_SOURCES = ["env:GH_TOKEN", "env:GITHUB_TOKEN", "gh-cli"]


def _fail(message: str) -> None:
    typer.echo(f"[colab] {message}", err=True)
    raise typer.Exit(code=2)


def _repo_url(repo: str) -> str:
    """``owner/name`` -> its GitHub https URL; an https URL is kept as given.

    Raises:
      typer.Exit: For ssh/http URLs, URLs with credentials in them, or
        anything else (exit code 2).
    """
    if _SHORTHAND.fullmatch(repo):
        return f"https://{_GITHUB_HOST}/{repo}.git"
    parsed = urlparse(repo)
    if parsed.scheme != "https" or not parsed.hostname:
        _fail(f"Unsupported repository {repo!r}: use owner/name or an https:// URL.")
    if parsed.username or parsed.password:
        _fail("Do not put credentials in the URL; use --token instead.")
    return repo


def _default_dest(url: str) -> str:
    name = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return f"/content/{name}"


def _basic_auth(token: str) -> str:
    """The value GitHub accepts for a token over https Basic auth."""
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _resolve_token(token: Optional[str], url: str, history) -> Optional[str]:
    if token == "none":
        return None
    if token is None:
        if urlparse(url).hostname != _GITHUB_HOST:
            return None
        return resolve_first("GH_TOKEN", _AUTO_TOKEN_SOURCES, history)
    value = read_source("GH_TOKEN", parse_source(token))
    if value is None:
        _fail(f"No token from {token}.")
    history.add_redaction(value)
    return value


def clone(
    repo: Annotated[
        str, typer.Argument(help="owner/name (GitHub) or an https:// git URL")
    ],
    dest: Annotated[
        Optional[str],
        typer.Argument(help="Directory on the runtime (default: /content/<name>)"),
    ] = None,
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    branch: Annotated[
        Optional[str], typer.Option("--branch", "-b", help="Branch or tag to check out")
    ] = None,
    depth: Annotated[
        Optional[int], typer.Option("--depth", help="Shallow clone with this many commits")
    ] = None,
    token: Annotated[
        Optional[str],
        typer.Option(
            "--token",
            help=(
                "Token for a private repository: env (GH_TOKEN), env:VAR, "
                "prompt, gh-cli, or none. Default for github.com: GH_TOKEN, "
                "GITHUB_TOKEN, then your local `gh` login, if any."
            ),
        ),
    ] = None,
):
    """Clone a git repository onto the runtime.

    A token is sent as an HTTP header for this one git command only: it is
    not put in the URL, the command line or .git/config, and the history log
    stores *** in its place.
    """
    from colab_cli.common import state

    if depth is not None and depth < 1:
        _fail("--depth must be at least 1.")
    url = _repo_url(repo)
    dest = dest or _default_dest(url)
    name = state.resolve_session(session)
    value = _resolve_token(token, url, state.history)

    args = ["git", "clone"]
    if branch:
        args += ["--branch", branch]
    if depth:
        args += ["--depth", str(depth)]
    args += [url, dest]

    env = {"GIT_TERMINAL_PROMPT": "0"}
    if value:
        header = _basic_auth(value)
        state.history.add_redaction(header)
        # GIT_CONFIG_* applies the setting to this process only (git >= 2.31).
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {header}",
            }
        )

    code = (
        "import os, subprocess\n"
        f"_r = subprocess.run({args!r}, capture_output=True, text=True, "
        f"env={{**os.environ, **{env!r}}})\n"
        "print(_r.stdout + _r.stderr, end='')\n"
        "if _r.returncode:\n"
        "    raise RuntimeError(f'git clone failed (exit {_r.returncode})')\n"
        f"print('Cloned into', {dest!r})\n"
    )
    typer.echo(f"[colab] Cloning {url} on {name}...")
    exit_if_failed(run_automation(name, "clone", code))


def register(app: typer.Typer):
    app.command()(clone)
