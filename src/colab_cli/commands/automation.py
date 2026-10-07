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

import datetime
import os
import sys
import threading
from typing import Callable, Optional, List
import typer
from rich.console import Console
from typing_extensions import Annotated

from colab_cli.runtime import ColabRuntime
from colab_cli.contents import ContentsClient
from colab_cli.drive_auth import (
    DRIVE_AUTH_TIMEOUT_SEC,
    mount_code,
    perform_drive_authorization,
    send_colab_reply,
)
from colab_cli.utils import render_display_data

_console = Console()


# Default execute() timeout for human-in-the-loop automations (auth /
# drivemount). The kernel goes silent while the user completes a browser
# OAuth flow, which can routinely take 30s+; the upstream 10s default
# raises ``TimeoutError`` mid-flow even though the mount actually succeeds.
# 10 minutes matches ``drive.mount(timeout_ms=...)`` so the kernel-side
# ``blocking_request`` does not give up at 120s while the user is still in
# the browser (upstream issue #113).
INTERACTIVE_AUTOMATION_TIMEOUT_SEC = DRIVE_AUTH_TIMEOUT_SEC


def make_drivefs_hook(
    state,
    session_state,
    *,
    authuser: str = "0",
    rewrite_auth_url: bool = False,
    runner: Optional[Callable[[Callable[[], None]], None]] = None,
):
    """Build the ``colab_request`` hook that answers ``dfs_ephemeral`` mounts.

    The handshake (HTTP propagation, browser prompt, stdin reply) runs off the
    websocket thread. Doing it inside ``on_message`` blocks the recv loop for
    the whole consent wait, so the kernel's execute reply cannot be delivered
    and a late Enter looks like a hang. ``runner`` exists so tests can run the
    worker inline; production starts a daemon thread.
    """

    def drivefs_hook(deserialize_msg, wsclient):
        content = deserialize_msg.get("content") or {}
        request = content.get("request") or {}
        if request.get("authType") != "dfs_ephemeral":
            return False
        msg_id = (deserialize_msg.get("metadata") or {}).get("colab_msg_id")
        state.history.log_event(
            session_state.name,
            "colab_request",
            {"type": "dfs_ephemeral", "colab_msg_id": msg_id},
        )

        def work():
            try:
                from colab_cli.auth import get_credentials

                http = get_credentials(
                    state.client_oauth_config, provider=state.auth_provider
                )
                perform_drive_authorization(
                    colab_domain=state.client.colab_domain,
                    endpoint=session_state.endpoint,
                    http=http,
                    wsclient=wsclient,
                    deserialize_msg=deserialize_msg,
                    msg_id=msg_id,
                    authuser=authuser,
                    rewrite_auth_url=rewrite_auth_url,
                    on_event=lambda ev, payload: state.history.log_event(
                        session_state.name, ev, payload
                    ),
                )
            except Exception as exc:
                typer.echo(f"[colab] Drive authorization failed: {exc}", err=True)
                try:
                    send_colab_reply(wsclient, deserialize_msg, msg_id, error=str(exc))
                except Exception:
                    pass

        if runner is not None:
            runner(work)
        else:
            threading.Thread(target=work, daemon=True, name="colab-drive-auth").start()
        return True

    return drivefs_hook


def run_automation(
    name: str,
    op: str,
    code: str,
    allow_stdin: bool = False,
    path: str = None,
    timeout: Optional[float] = None,
    authuser: str = "0",
    rewrite_auth_url: bool = False,
):
    from colab_cli.common import state

    s = state.get_session(name)
    runtime = ColabRuntime(s.url, s.token, session_name=s.name, history=state.history)
    runtime.colab_request_hook = make_drivefs_hook(
        state,
        s,
        authuser=authuser,
        rewrite_auth_url=rewrite_auth_url,
    )
    try:
        s.running = f"automation({op})"
        s.last_execution = (
            f"automation:{op}",
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        state.store.add(s)

        if op == "drivemount":
            state.history.log_event(
                name, "automation", {"op": "drivemount", "path": path, "code": code}
            )
        else:
            state.history.log_event(name, "automation", {"op": op, "code": code})

        outputs = runtime.execute_code(code, allow_stdin=allow_stdin, timeout=timeout)
        state.history.log_event(
            name, "automation_result", {"op": op, "outputs": outputs}
        )

        for out in outputs:
            if "text" in out:
                sys.stdout.write(out["text"])
            elif "data" in out:
                text = render_display_data(out["data"])
                if text is not None:
                    _console.print(text)
            elif out.get("output_type") == "error":
                ename = out.get("ename", "Error")
                evalue = out.get("evalue", "")
                tb = out.get("traceback", [])
                if tb:
                    sys.stderr.write("".join(tb) + "\n")
                else:
                    sys.stderr.write(f"{ename}: {evalue}\n")
    finally:
        s.running = None
        state.store.add(s)
        runtime.stop()


def auth(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Authenticate with Google on the VM"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    code = "import os\nos.environ['USE_AUTH_EPHEM'] = '0'\nfrom google.colab import auth\nauth.authenticate_user()"
    typer.echo(f"[colab] Starting Google Auth flow on {name}...")
    run_automation(
        name,
        "auth",
        code,
        allow_stdin=True,
        timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
    )


def drivemount(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    authuser: Annotated[
        Optional[str],
        typer.Option(
            "--authuser",
            help=(
                "Account index to select on the Drive consent screen. "
                "Appended to the authorization URL. Set this only when the "
                "chooser opens the wrong Google account. The propagation "
                "request's authuser parameter does not by itself fix a "
                "multi-account HTTP 400."
            ),
        ),
    ] = None,
    path: Annotated[str, typer.Argument(help="Mount path")] = "/content/drive",
):
    """Mount Google Drive at path"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    explicit_authuser = authuser is not None
    code = mount_code(path, timeout_ms=INTERACTIVE_AUTOMATION_TIMEOUT_SEC * 1000)
    typer.echo(f"[colab] Mounting Google Drive to '{path}' on {name}...")
    run_automation(
        name,
        "drivemount",
        code,
        allow_stdin=True,
        path=path,
        timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
        authuser=authuser if explicit_authuser else "0",
        rewrite_auth_url=explicit_authuser,
    )


def install(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    packages: Annotated[
        Optional[List[str]], typer.Argument(help="Packages to install")
    ] = None,
    requirement: Annotated[
        Optional[str], typer.Option("-r", "--requirement", help="Requirements file")
    ] = None,
):
    """Install python packages on the VM"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    if not packages and not requirement:
        typer.echo("[colab] No packages or requirements specified.")
        raise typer.Exit(1)

    commands = []
    if requirement:
        if not os.path.isfile(requirement):
            typer.echo(f"[colab] Requirements file '{requirement}' not found locally.")
            raise typer.Exit(1)
        contents = ContentsClient(state.get_session(name))
        remote_path = f"content/{os.path.basename(requirement)}"
        contents.upload(requirement, remote_path)
        commands.extend(["-r", f"/{remote_path}"])
    if packages:
        commands.extend(packages)

    cmd_str = ", ".join(f"'{c}'" for c in commands)
    code = f"""
import subprocess, sys
def install():
    packages = [{cmd_str}]
    try:
        subprocess.check_call(['uv', 'pip', 'install', '--system'] + packages)
        print('Installation Complete (via uv)!')
    except:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + packages)
        print('Installation Complete (via pip)!')
install()
"""
    typer.echo(f"[colab] Installing packages on {name} (preferring uv)...")
    run_automation(name, "install", code)


def register(app: typer.Typer):
    app.command(hidden=True)(auth)
    app.command()(drivemount)
    app.command()(install)
