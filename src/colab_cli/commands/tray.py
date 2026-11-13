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

"""`colab tray`: a tray icon and panel for the active runtimes (docs/09).

The one long-running exception to the fire-and-forget rule (AGENTS.md 7): it
runs only when started, as its own process. The GUI needs the optional
`[tray]` extra (PySide6); `--snapshot` works without it.
"""

import os
import platform
import subprocess
import sys
import time

import filelock
import typer
from typing_extensions import Annotated

from colab_cli.common import colab_argv
from colab_cli.results import JsonEmitter
from colab_cli.tray_model import card_model, collect_snapshot, cu_model, icon_state

INSTALL_HINT = (
    'uv tool install --force "google-colab-cli[tray] @ '
    'git+https://github.com/arlinamid/google-colab-cli"'
)


def _config_dir() -> str:
    from colab_cli.common import state

    if state.config_path:
        return os.path.dirname(os.path.abspath(state.config_path))
    return os.path.join(os.path.expanduser("~"), ".config", "colab-cli")


def _gui_problem(system=None, env=None, release=None):
    """Why no tray can be shown here, or None."""
    system = system or sys.platform
    env = os.environ if env is None else env
    release = platform.release() if release is None else release
    if system.startswith("linux"):
        if "microsoft" in release.lower():
            return "WSL has no system tray; run `colab tray` on the Windows side."
        if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
            return "no display (DISPLAY / WAYLAND_DISPLAY is not set)."
    return None


def _load_gui():
    """The GUI entry point; raises ImportError without the [tray] extra."""
    import PySide6  # noqa: F401

    from colab_cli.tray.app import run_tray

    return run_tray


def _print_snapshot(snap, now: float, interval: float) -> None:
    icon = icon_state(snap, now=now, interval=interval)
    typer.echo(f"[{icon['level']}] {icon['text']}")
    for entry in snap.get("sessions", []):
        c = card_model(entry, now)
        line = f"  {c['name']:<16} {c['hardware']:<5} {c['state']}"
        if c["percent"] is not None and c["state"] in ("running", "stalled"):
            line += f"  {c['percent']:g}%"
        elif c["last_output_age_s"] is not None and c["state"] in ("running", "stalled"):
            line += f"  last output {c['last_output_age_s']:.0f} s ago"
        if c["epoch"]:
            line += f"  epoch {c['epoch'][0]}/{c['epoch'][1]}"
        if c["cell"]:
            line += f"  cell {c['cell'][0]}/{c['cell'][1]}"
        if c["metric"]:
            line += f"  {c['metric']['name']}={c['metric']['value']}"
        if c["diagnosis"]:
            line += f"  ({c['diagnosis']})"
        for f in c.get("forwards") or []:
            line += f"  forward {f['local_port']}" + ("" if f.get("listening") else " (not answering)")
        typer.echo(line)
    cu = cu_model(snap.get("account"))
    if cu:
        left = f", about {cu['hours_left']} h left at this rate (estimated)" if cu["hours_left"] is not None else ""
        typer.echo(f"  balance {cu['balance_cu']:.1f} CU, {cu['rate_cu_per_hour']:g} CU/h{left}")


def _autostart(choice: str) -> int:
    from colab_cli.tray import autostart

    if choice not in ("on", "off", "status"):
        typer.echo(f"[colab] --autostart takes on, off or status, not {choice!r}.", err=True)
        return 2
    try:
        if choice == "on":
            where = autostart.enable([*colab_argv(), "tray"])
            typer.echo(f"[colab] The tray starts when you sign in ({where}).")
        elif choice == "off":
            done = autostart.disable()
            typer.echo("[colab] The tray no longer starts when you sign in." if done
                       else "[colab] The tray was not set to start when you sign in.")
        else:
            cmd = autostart.status()
            typer.echo(f"autostart: on ({cmd})" if cmd else "autostart: off")
    except OSError as e:
        typer.echo(f"[colab] Could not change the sign-in start: {e}", err=True)
        return 1
    return 0


def tray(
    interval: Annotated[float, typer.Option("--interval", help="Seconds between refreshes.")] = 30.0,
    detach: Annotated[bool, typer.Option("--detach", help="Start the tray in the background and return.")] = False,
    snapshot: Annotated[
        bool, typer.Option("--snapshot", help="Print what the tray would show once and exit (no GUI needed).")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", envvar="COLAB_CLI_JSON", help="With --snapshot: print JSON.")] = False,
    autostart: Annotated[
        str,
        typer.Option(
            "--autostart", metavar="on|off|status",
            help="Start the tray when you sign in (Windows, macOS, Linux), stop doing so, or show the setting; then exit.",
        ),
    ] = "",
):
    """Show the active runtimes in a tray icon and panel.

    The panel shows each runtime's job, progress and main metric, the compute
    units left (estimated), and starts profiles (`colab profile`). The tray
    never keeps a runtime alive: it sends no keep-alive and reads a runtime's
    job files only while a job runs there (otherwise every 5 minutes).
    Needs the [tray] extra; --snapshot works without it.
    """
    from colab_cli.common import state

    if autostart:
        raise typer.Exit(_autostart(autostart))
    if snapshot:
        now = time.time()
        snap = collect_snapshot(state, now=now)
        if json_output:
            JsonEmitter(sys.stdout).emit({
                **snap,
                "icon": icon_state(snap, now=now, interval=interval),
                "cards": [card_model(e, now) for e in snap.get("sessions", [])],
                "cu": cu_model(snap.get("account")),
            })
        else:
            _print_snapshot(snap, now, interval)
        raise typer.Exit(0 if snap["ok"] else 1)

    problem = _gui_problem()
    if problem:
        typer.echo(f"[colab] Cannot show a tray icon: {problem} `colab tray --snapshot` still works.", err=True)
        raise typer.Exit(2)
    try:
        run_tray = _load_gui()
    except ImportError as e:
        typer.echo(f"[colab] The tray needs the [tray] extra ({e}). Install it with:\n  {INSTALL_HINT}", err=True)
        raise typer.Exit(2)

    if detach:
        argv = [*colab_argv(), "tray", "--interval", f"{interval:g}"]
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)
        typer.echo("[colab] The tray is starting in the background.")
        return

    os.makedirs(_config_dir(), exist_ok=True)
    lock = filelock.FileLock(os.path.join(_config_dir(), "tray.lock"))
    try:
        lock.acquire(timeout=0)
    except filelock.Timeout:
        typer.echo("[colab] The tray is already running.", err=True)
        raise typer.Exit(1)
    try:
        code = run_tray(state=state, interval=interval)
    finally:
        lock.release()
    raise typer.Exit(code or 0)


def register(app: typer.Typer):
    app.command("tray")(tray)
