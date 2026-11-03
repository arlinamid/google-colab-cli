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

"""``colab share``: a public URL for a port on the runtime (cloudflared/ngrok).

Unlike ``colab forward`` (private, only your machine), this makes the port
reachable by anyone with the URL. The tunnel program runs on the runtime, not
locally, so nothing is left running on your machine. By default the command
stays in the foreground and keeps the runtime alive (the backend only counts
kernel activity, so tunnel traffic alone would let it idle out); Ctrl+C stops
sharing. ``--detach`` leaves the tunnel running and exits.

Both tools are installed from their vendors' signed apt repositories, so apt
verifies the packages' signatures.
"""

from typing import Optional

import typer
from typing_extensions import Annotated

from colab_cli.commands.automation import exit_if_failed, run_automation
from colab_cli.keepalive import keep_alive_until_interrupted
from colab_cli.secrets import parse_source, read_source, resolve_first

_INSTALL_TIMEOUT_SEC = 600
_URL_WAIT_SEC = 60

# Install scripts follow each vendor's documented apt setup, with the key
# scoped to its own repository (signed-by) and only that list refreshed.
_APT_UPDATE_ONE = (
    "apt-get update -qq -o Dir::Etc::sourcelist=sources.list.d/{name}.list "
    "-o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0\n"
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {name}\n"
)
_TOOLS = {
    "cloudflared": {
        "install": (
            "set -e\n"
            "mkdir -p --mode=0755 /usr/share/keyrings\n"
            "curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg "
            "-o /usr/share/keyrings/cloudflare-main.gpg\n"
            "echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] "
            "https://pkg.cloudflare.com/cloudflared any main' "
            "> /etc/apt/sources.list.d/cloudflared.list\n"
            + _APT_UPDATE_ONE.format(name="cloudflared")
        ),
        "argv": lambda port: [
            "cloudflared", "tunnel", "--url", f"http://localhost:{port}",
            "--no-autoupdate",
        ],
        "url_regex": r"(https://[a-z0-9-]+\.trycloudflare\.com)",
    },
    "ngrok": {
        "install": (
            "set -e\n"
            "mkdir -p --mode=0755 /usr/share/keyrings\n"
            "curl -fsSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc "
            "-o /usr/share/keyrings/ngrok.asc\n"
            "echo 'deb [signed-by=/usr/share/keyrings/ngrok.asc] "
            "https://ngrok-agent.s3.amazonaws.com buster main' "
            "> /etc/apt/sources.list.d/ngrok.list\n"
            + _APT_UPDATE_ONE.format(name="ngrok")
        ),
        "argv": lambda port: ["ngrok", "http", str(port), "--log", "stdout"],
        "url_regex": r"url=(https://\S+)",
    },
}

# Kernel-side helper shared by start, stop and list. A share is recorded as
# /tmp/colab-share-<port>.pid holding "<pid> <tool> <url>".
_KERNEL_HELPERS = """\
import glob, os, re, shutil, signal, subprocess, time

def _shares():
    for f in sorted(glob.glob('/tmp/colab-share-*.pid')):
        port = f[len('/tmp/colab-share-'):-len('.pid')]
        pid, tool, url = (open(f).read().split(' ', 2) + ['', ''])[:3]
        yield f, port, int(pid), tool, url.strip()

def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _stop(port):
    for f, p, pid, tool, url in list(_shares()):
        if port is not None and p != str(port):
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
            print(f'Stopped {tool} on port {p}')
        except ProcessLookupError:
            pass
        os.remove(f)
"""

_START = """\
_port, _tool, _install = {port!r}, {tool!r}, {install!r}
_cmd = {argv!r}
_extra_env = {env!r}
_stop(_port)
if not shutil.which(_tool):
    print(f'Installing {{_tool}} from its signed apt repository...')
    _r = subprocess.run(['bash', '-c', _install], capture_output=True, text=True)
    if _r.returncode:
        print(_r.stdout + _r.stderr)
        raise RuntimeError(f'installing {{_tool}} failed (exit {{_r.returncode}})')
_log = f'/tmp/colab-share-{{_port}}.log'
_pidf = f'/tmp/colab-share-{{_port}}.pid'
with open(_log, 'w') as _f:
    _p = subprocess.Popen(_cmd, stdout=_f, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, start_new_session=True,
                          env={{**os.environ, **_extra_env}})
open(_pidf, 'w').write(f'{{_p.pid}} {{_tool}} ')
_url, _deadline = None, time.time() + {wait}
while time.time() < _deadline and _p.poll() is None:
    _m = re.search({url_regex!r}, open(_log).read())
    if _m:
        _url = _m.group(1)
        break
    time.sleep(0.5)
if not _url:
    print(''.join(open(_log).readlines()[-15:]))
    _stop(_port)
    raise RuntimeError(f'{{_tool}} did not report a public URL')
open(_pidf, 'w').write(f'{{_p.pid}} {{_tool}} {{_url}}')
print(f'Public URL: {{_url}}  ->  port {{_port}} on the runtime')
"""

_LIST = """\
_any = False
for f, port, pid, tool, url in _shares():
    _any = True
    state = 'running' if _alive(pid) else 'stopped'
    print(f'port {port}  {tool}  {url}  ({state})')
if not _any:
    print('No shares.')
"""


def _check_port(port: str) -> int:
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        typer.echo(f"[colab] Invalid port {port!r}: expected 1-65535.", err=True)
        raise typer.Exit(code=2)
    return int(port)


def _ngrok_token(token: Optional[str], history) -> str:
    if token is None:
        value = resolve_first(
            "NGROK_AUTHTOKEN", ["env:NGROK_AUTHTOKEN", "prompt"], history
        )
    else:
        value = read_source("NGROK_AUTHTOKEN", parse_source(token))
        if value is not None:
            history.add_redaction(value)
    if value is None:
        typer.echo(
            "[colab] ngrok needs an authtoken: set NGROK_AUTHTOKEN or use "
            "--token prompt (https://dashboard.ngrok.com/get-started/your-authtoken).",
            err=True,
        )
        raise typer.Exit(code=2)
    return value


def _stop_code(port: Optional[int]) -> str:
    return _KERNEL_HELPERS + f"_stop({port!r})\n"


def share(
    port: Annotated[
        Optional[str], typer.Argument(help="Port on the runtime to share")
    ] = None,
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    via: Annotated[
        str,
        typer.Option(
            "--via",
            help="cloudflared (no account needed) or ngrok (needs an authtoken).",
        ),
    ] = "cloudflared",
    token: Annotated[
        Optional[str],
        typer.Option(
            "--token",
            help=(
                "ngrok authtoken source: env (NGROK_AUTHTOKEN), env:VAR or "
                "prompt. Default: NGROK_AUTHTOKEN, then a hidden prompt."
            ),
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach",
            help=(
                "Start the tunnel and exit. Without keep-alive the runtime "
                "may idle out; see `colab keepalive`."
            ),
        ),
    ] = False,
    list_: Annotated[
        bool, typer.Option("--list", help="List the shares on the runtime.")
    ] = False,
    stop: Annotated[
        bool,
        typer.Option("--stop", help="Stop sharing PORT, or every share without PORT."),
    ] = False,
):
    """Share a port on the runtime at a public URL (cloudflared or ngrok).

    Anyone with the URL can reach the port. For access from only your own
    machine, use `colab forward` instead. Stays in the foreground and keeps
    the runtime alive; Ctrl+C stops sharing.
    """
    from colab_cli.common import state

    if via not in _TOOLS:
        typer.echo(f"[colab] --via must be one of: {', '.join(_TOOLS)}.", err=True)
        raise typer.Exit(code=2)
    port_num = _check_port(port) if port is not None else None
    name = state.resolve_session(session)

    if list_:
        exit_if_failed(run_automation(name, "share-list", _KERNEL_HELPERS + _LIST))
        return
    if stop:
        exit_if_failed(run_automation(name, "share-stop", _stop_code(port_num)))
        return
    if port_num is None:
        typer.echo("[colab] Missing PORT to share.", err=True)
        raise typer.Exit(code=2)

    env = {}
    if via == "ngrok":
        env["NGROK_AUTHTOKEN"] = _ngrok_token(token, state.history)
    tool = _TOOLS[via]
    code = _KERNEL_HELPERS + _START.format(
        port=port_num,
        tool=via,
        install=tool["install"],
        argv=tool["argv"](port_num),
        env=env,
        wait=_URL_WAIT_SEC,
        url_regex=tool["url_regex"],
    )
    typer.echo(
        f"[colab] Warning: port {port_num} on '{name}' becomes publicly "
        "reachable by anyone with the URL."
    )
    outputs = run_automation(
        name, "share", code, timeout=_INSTALL_TIMEOUT_SEC
    )
    exit_if_failed(outputs)

    s = state.get_session(name)
    if detach:
        typer.echo(
            "[colab] Tunnel left running. The runtime may idle out after "
            f"~90 min without kernel activity; keep it up with `colab keepalive -s {name}`, "
            f"stop with `colab share -s {name} --stop {port_num}`."
        )
        return
    typer.echo(
        "[colab] Keeping the runtime alive. Press Ctrl+C to stop sharing."
    )
    keep_alive_until_interrupted(state.client, s.endpoint)
    run_automation(name, "share-stop", _stop_code(port_num))


def keepalive(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    minutes: Annotated[
        Optional[float],
        typer.Option("--minutes", help="Stop after this many minutes (default: Ctrl+C)."),
    ] = None,
):
    """Keep a runtime alive until Ctrl+C (or --minutes), without running code.

    The backend only counts kernel activity as use. Run this while a detached
    tunnel or a background job should keep the runtime up.
    """
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.get_session(name)
    typer.echo(f"[colab] Keeping '{name}' alive. Press Ctrl+C to stop.")
    duration = minutes * 60 if minutes is not None else None
    keep_alive_until_interrupted(state.client, s.endpoint, duration=duration)


def register(app: typer.Typer):
    app.command()(share)
    app.command()(keepalive)
