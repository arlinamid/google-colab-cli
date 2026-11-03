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

"""``colab hf``: Hugging Face login and downloads on the runtime."""

from typing import Optional

import typer
from typing_extensions import Annotated

from colab_cli.commands.automation import exit_if_failed, run_automation
from colab_cli.secrets import (
    build_secret_prelude,
    parse_source,
    read_source,
    resolve_first,
)

hf_app = typer.Typer(
    help="Hugging Face on the runtime: log in, download models and datasets.",
    no_args_is_help=True,
)

_REPO_TYPES = ("model", "dataset", "space")
# Downloads outlast the kernel client's short default request timeout.
_DOWNLOAD_TIMEOUT_SEC = 3600
# Tried in order when --token is not given.
_DEFAULT_TOKEN_SOURCES = ["env:HF_TOKEN", "hf-cli", "prompt"]
_IMPORT_HUB = (
    "try:\n"
    "    import huggingface_hub\n"
    "except ImportError:\n"
    "    raise ImportError('huggingface_hub is not installed on the runtime; "
    "run: colab install huggingface_hub') from None\n"
)


def _session_name(session: Optional[str]) -> str:
    from colab_cli.common import state

    return state.resolve_session(session)


@hf_app.command("login")
def login(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    token: Annotated[
        Optional[str],
        typer.Option(
            "--token",
            help=(
                "Where to read the token: env (HF_TOKEN), env:VAR, prompt or "
                "hf-cli. Default: HF_TOKEN, then your local `hf` login, then "
                "a hidden prompt."
            ),
        ),
    ] = None,
):
    """Log the runtime in to Hugging Face (sets HF_TOKEN in the kernel).

    The token is never put on the command line or in the history log.
    """
    from colab_cli.common import state

    name = _session_name(session)
    if token is None:
        value = resolve_first("HF_TOKEN", _DEFAULT_TOKEN_SOURCES, state.history)
    else:
        value = read_source("HF_TOKEN", parse_source(token))
        if value is not None:
            state.history.add_redaction(value)
    if value is None:
        typer.echo("[colab] No Hugging Face token found.", err=True)
        raise typer.Exit(code=2)

    code = (
        build_secret_prelude({"HF_TOKEN": value})
        + _IMPORT_HUB
        + "huggingface_hub.login(token=os.environ['HF_TOKEN'], "
        "add_to_git_credential=False)\n"
        "print('Logged in to Hugging Face as', "
        "huggingface_hub.whoami().get('name'))\n"
    )
    exit_if_failed(run_automation(name, "hf-login", code, session_kernel=True))


@hf_app.command("download")
def download(
    repo_id: Annotated[str, typer.Argument(help="Repository id, e.g. google/gemma-3-1b-it")],
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    repo_type: Annotated[
        str, typer.Option("--repo-type", help="model, dataset or space")
    ] = "model",
    revision: Annotated[
        Optional[str], typer.Option("--revision", help="Branch, tag or commit")
    ] = None,
    local_dir: Annotated[
        Optional[str],
        typer.Option(
            "--local-dir",
            help="Directory on the runtime (default: the Hugging Face cache).",
        ),
    ] = None,
):
    """Download a model, dataset or space snapshot onto the runtime.

    Gated or private repositories need `colab hf login` first.
    """
    if repo_type not in _REPO_TYPES:
        typer.echo(
            f"[colab] --repo-type must be one of: {', '.join(_REPO_TYPES)}.",
            err=True,
        )
        raise typer.Exit(code=2)
    name = _session_name(session)
    # repr() makes every user value a safe Python literal (AGENTS.md #20).
    code = (
        _IMPORT_HUB
        + "path = huggingface_hub.snapshot_download("
        f"repo_id={repo_id!r}, repo_type={repo_type!r}, "
        f"revision={revision!r}, local_dir={local_dir!r})\n"
        "print('Downloaded to', path)\n"
    )
    typer.echo(f"[colab] Downloading {repo_id} on {name}...")
    exit_if_failed(
        run_automation(name, "hf-download", code, timeout=_DOWNLOAD_TIMEOUT_SEC)
    )


def register(app: typer.Typer):
    app.add_typer(hf_app, name="hf")
