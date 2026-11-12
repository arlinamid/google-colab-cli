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

import os
import sys
from typing import Optional

import click
import typer
from typer.core import TyperGroup
from typing_extensions import Annotated

from colab_cli import auto_update
from colab_cli.auth import AuthProvider
from colab_cli.common import state, setup_logging
from colab_cli.commands import (
    session,
    execution,
    files,
    automation,
    git,
    hf,
    inspect,
    job,
    run,
    secret,
    share,
    ssh,
    utility,
    usage,
)


class AlphabeticalGroup(TyperGroup):
    """A `TyperGroup` that lists subcommands alphabetically in `--help` output.

    Subcommands are registered in functional groups (session, execution, files,
    automation, utility), but users discovering the CLI via `colab --help` /
    `colab help` benefit from a deterministic, alphabetical listing.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(super().list_commands(ctx))


app = typer.Typer(
    help="Colab CLI",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    cls=AlphabeticalGroup,
)


@app.callback()
def callback(
    ctx: typer.Context,
    client_oauth_config: Annotated[
        str,
        typer.Option(
            "-c", "--client-oauth-config", help="Path to client OAuth config JSON file"
        ),
    ] = os.path.expanduser("~/.colab-cli-oauth-config.json"),
    config: Annotated[
        Optional[str],
        typer.Option(
            "--config",
            help="Path to session state file (~/.config/colab-cli/sessions.json)",
        ),
    ] = None,
    logtostderr: Annotated[
        bool, typer.Option("--logtostderr", help="Log all output to stderr")
    ] = False,
    auth: Annotated[
        AuthProvider,
        typer.Option(
            "--auth",
            help=(
                "Authentication strategy to use: 'oauth2' (public InstalledAppFlow),"
                " or 'adc' (Application Default Credentials)."
            ),
            case_sensitive=False,
        ),
    ] = AuthProvider.OAUTH2,
):
    """
    Colab CLI global configuration.
    """
    # ANSI colors and cursor control in cmd.exe / Windows PowerShell 5.1.
    # No-op on Linux, macOS, and consoles that already have virtual-terminal
    # processing enabled.
    from colab_cli.terminal import enable_windows_virtual_terminal

    enable_windows_virtual_terminal()
    state.client_oauth_config = client_oauth_config
    state.config_path = config
    state.logtostderr = logtostderr
    state.auth_provider = auth
    setup_logging(logtostderr)

    # Daily fetch + cached banner on every invocation.
    #
    # Suppress the banner for short-lived informational subcommands so their
    # output stays clean and machine-parseable:
    #   - `update`: runs its own check + announce; would duplicate the banner.
    #   - `version`, `log`, `pay`, `help`, `url`: pure-display commands whose
    #     output users routinely pipe / scrape (e.g. `colab url -s s1 | xclip`);
    #     a stochastic upgrade banner injected once a day would corrupt those
    #     pipelines.
    #   - `whoami`: developer-only debugging tool; banner would obscure the
    #     auth/scope info the user invoked it to see.
    #   - `ssh` when stdout is not a terminal: piped `-- CMD` output and the
    #     ProxyCommand's ssh byte stream must stay byte-exact.
    _AUTO_UPDATE_SUPPRESSED = {
        "update",
        "version",
        "log",
        "pay",
        "help",
        "url",
        "whoami",
        "readme",
        "README",
        "skill",
        "SKILL",
    }
    subcommand = ctx.invoked_subcommand
    piped_ssh = subcommand == "ssh" and not execution.is_stdout_tty()
    if subcommand not in _AUTO_UPDATE_SUPPRESSED and not piped_ssh:
        auto_update.run_background_check()


@app.command(name="help")
def help_command(
    ctx: typer.Context,
    command: Annotated[
        Optional[str], typer.Argument(help="Command to show help for")
    ] = None,
):
    """
    Show help for a command.
    """
    if not command:
        typer.echo(ctx.parent.get_help())
        return

    group = ctx.parent.command
    cmd = group.get_command(ctx, command)
    if cmd is None:
        typer.echo(f"No such command '{command}'.", err=True)
        raise typer.Exit(code=2)

    with click.Context(cmd, info_name=command, parent=ctx.parent) as cmd_ctx:
        typer.echo(cmd.get_help(cmd_ctx))


# Register subcommands
session.register(app)
execution.register(app)
files.register(app)
automation.register(app)
git.register(app)
hf.register(app)
inspect.register(app)
job.register(app)
run.register(app)
secret.register(app)
share.register(app)
ssh.register(app)
utility.register(app)
usage.register(app)


def _connection_lost_message(name: Optional[str]) -> str:
    """Explain a dropped kernel connection in one line, not a traceback."""
    from colab_cli.common import state

    try:
        still_recorded = name is not None and state.store.get(name) is not None
    except Exception:
        still_recorded = True
    if not still_recorded:
        return f"[colab] Session '{name}' was stopped, so the running command ended."
    return (
        f"[colab] Lost the connection to session '{name}'. The runtime may have "
        f"been stopped or disconnected; check it with `colab status -s {name}`."
    )


def configure_stdio() -> None:
    """Use UTF-8 for redirected stdin/stdout/stderr (see utf8_text_stream)."""
    from colab_cli.utils import utf8_text_stream

    sys.stdin = utf8_text_stream(sys.stdin)
    sys.stdout = utf8_text_stream(sys.stdout)
    sys.stderr = utf8_text_stream(sys.stderr)


def main():
    from colab_cli.runtime import SessionConnectionLost

    configure_stdio()
    try:
        app()
    except SessionConnectionLost as e:
        typer.echo(_connection_lost_message(e.session_name), err=True)
        raise SystemExit(1)
    except TimeoutError as e:
        # Raised by ColabRuntime when --timeout passes (the kernel has been
        # interrupted), or by a network call that gave up.
        typer.echo(f"[colab] {e or 'Timed out.'}", err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
