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

"""``colab secret set``: put tokens into a session's kernel environment."""

from typing import List, Optional

import typer
from typing_extensions import Annotated

from colab_cli.commands.automation import exit_if_failed, run_automation
from colab_cli.secrets import build_secret_prelude, resolve_secrets

secret_app = typer.Typer(
    help="Set secrets (tokens) in a session's kernel environment.",
    no_args_is_help=True,
)


@secret_app.command("set")
def set_secrets(
    specs: Annotated[
        List[str],
        typer.Argument(
            metavar="NAME[=SOURCE]...",
            help=(
                "Secrets to set. SOURCE is env (the local NAME variable, "
                "default), env:VAR, prompt, hf-cli or gh-cli. Values on the "
                "command line are refused."
            ),
        ),
    ],
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Set secrets in the kernel's os.environ for the rest of the session.

    The values never appear on the command line, and the history log stores
    *** in their place.
    """
    from colab_cli.common import state

    name = state.resolve_session(session)
    values = resolve_secrets(specs, state.history)
    code = build_secret_prelude(values) + f"print('Set: ' + ', '.join({list(values)!r}))\n"
    exit_if_failed(run_automation(name, "secret", code))


def register(app: typer.Typer):
    app.add_typer(secret_app, name="secret")
