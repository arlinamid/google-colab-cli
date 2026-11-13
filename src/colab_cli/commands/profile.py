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

"""`colab profile`: saved ways to run something (docs/09_tray_and_profiles.md).

`colab profile run` drives the CLI's own subcommands (`colab run --detach`,
`colab job start`, `colab new`, `colab exec`, `colab forward`, `colab stop`)
as child processes with the same global options, so a profile does exactly
what those commands do and there is one code path. The tray starts profiles
the same way.
"""

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Dict, List, Optional

import typer
from typing_extensions import Annotated

from colab_cli.common import colab_argv
from colab_cli.profiles import (
    Profile,
    ProfileError,
    dump_toml,
    load_profiles,
    profile_summary,
    profiles_path,
    remove_profile,
    resolve_script,
    save_profile,
)
from colab_cli.results import SCHEMA, JsonEmitter

profile_app = typer.Typer(
    help="Saved ways to run something: a script as a background job, or a server in the kernel.",
    no_args_is_help=True,
)

JsonOpt = Annotated[bool, typer.Option("--json", envvar="COLAB_CLI_JSON", help="Print JSON.")]
READY_POLL_SEC = 2.0


def _path() -> str:
    from colab_cli.common import state

    return profiles_path(state.config_path)


def _load() -> Dict[str, Profile]:
    try:
        return load_profiles(_path())
    except ProfileError as e:
        typer.echo(f"[colab] {e}", err=True)
        raise typer.Exit(2)


def _get(name: str) -> Profile:
    profiles = _load()
    if name not in profiles:
        known = ", ".join(profiles) or "none yet (see `colab profile add`)"
        typer.echo(f"[colab] No profile '{name}' in {_path()}. Profiles: {known}.", err=True)
        raise typer.Exit(2)
    return profiles[name]


@profile_app.command("list")
def list_profiles(json_output: JsonOpt = False):
    """List the profiles."""
    profiles = _load()
    if json_output:
        JsonEmitter(sys.stdout).emit({
            "schema": SCHEMA, "type": "profiles", "path": _path(),
            "profiles": [{"name": n, **profile_summary(p)} for n, p in profiles.items()],
        })
        return
    if not profiles:
        typer.echo(f"[colab] No profiles in {_path()}. Add one with `colab profile add`.")
        return
    for name, p in profiles.items():
        s = profile_summary(p)
        extra = f"  forward {','.join(p.forward)}" if p.forward else ""
        typer.echo(f"{name:<20} {s['mode']:<6} {s['hardware']:<5} {p.script}{extra}")
        if p.description:
            typer.echo(f"{'':<20} {p.description}")


@profile_app.command("show")
def show(name: Annotated[str, typer.Argument(help="Profile name.")], json_output: JsonOpt = False):
    """Show one profile (with its defaults)."""
    p = _get(name)
    if json_output:
        JsonEmitter(sys.stdout).emit({"name": name, **p.model_dump()})
        return
    typer.echo(dump_toml({name: p.model_dump(exclude_none=True)}).rstrip())


@profile_app.command(
    "add", context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def add(
    name: Annotated[str, typer.Argument(help="Profile name.")],
    command: Annotated[
        Optional[List[str]], typer.Argument(help="SCRIPT [ARGS...] after --.")
    ] = None,
    mode: Annotated[str, typer.Option("--mode", help="job (background job) or kernel (colab exec).")] = "job",
    session: Annotated[Optional[str], typer.Option("-s", "--session", help="Fixed session name; reused while alive.")] = None,
    gpu: Annotated[Optional[str], typer.Option("--gpu", help="GPU type, e.g. T4.")] = None,
    tpu: Annotated[Optional[str], typer.Option("--tpu", help="TPU type.")] = None,
    high_mem: Annotated[bool, typer.Option("--high-mem", help="High-RAM machine shape.")] = False,
    budget: Annotated[Optional[float], typer.Option("--budget", help="Compute-unit limit (job mode).")] = None,
    secret: Annotated[Optional[List[str]], typer.Option("--secret", help="`colab secret` name. Repeatable.")] = None,
    env: Annotated[Optional[List[str]], typer.Option("--env", help="KEY=VALUE. Repeatable.")] = None,
    forward: Annotated[Optional[List[str]], typer.Option("--forward", help="Port to forward (kernel mode). Repeatable.")] = None,
    open_url: Annotated[Optional[str], typer.Option("--open", help="URL to open once it answers.")] = None,
    description: Annotated[Optional[str], typer.Option("--description", help="One line shown in lists and the tray.")] = None,
    keep_runtime: Annotated[bool, typer.Option("--keep-runtime", help="Keep the runtime after the run (stop_when_done = false).")] = False,
    force: Annotated[bool, typer.Option("--force", help="Replace a profile of the same name.")] = False,
):
    """Add a profile: `colab profile add NAME OPTIONS -- SCRIPT ARGS...`.

    For vars, ready URLs and other keys, edit profiles.toml (`colab profile path`).
    """
    if not command:
        typer.echo("[colab] Give the script after --: colab profile add NAME -- SCRIPT [ARGS].", err=True)
        raise typer.Exit(2)
    path = _path()
    if name in _load() and not force:
        typer.echo(f"[colab] Profile '{name}' exists; use --force to replace it.", err=True)
        raise typer.Exit(2)
    env_vars = {}
    for item in env or []:
        key, sep, value = item.partition("=")
        if not sep:
            typer.echo(f"[colab] --env wants KEY=VALUE, got {item!r}.", err=True)
            raise typer.Exit(2)
        env_vars[key] = value
    fields = {
        "script": os.path.abspath(os.path.expanduser(command[0])), "args": command[1:], "mode": mode,
        "session": session, "gpu": gpu, "tpu": tpu, "high_mem": high_mem, "budget": budget,
        "secrets": secret or [], "env": env_vars, "forward": forward or [], "open": open_url,
        "description": description, "stop_when_done": not keep_runtime,
    }
    try:
        p = Profile(**{k: v for k, v in fields.items() if v is not None})
    except Exception as e:
        typer.echo(f"[colab] Profile '{name}' is not valid: {e}", err=True)
        raise typer.Exit(2)
    save_profile(path, name, p)
    typer.echo(f"[colab] Saved profile '{name}' in {path}. Run it with `colab profile run {name}`.")


@profile_app.command("rm")
def rm(name: Annotated[str, typer.Argument(help="Profile name.")]):
    """Remove a profile."""
    if not remove_profile(_path(), name):
        typer.echo(f"[colab] No profile '{name}'.", err=True)
        raise typer.Exit(1)
    typer.echo(f"[colab] Removed profile '{name}'.")


@profile_app.command("path")
def path_command():
    """Print the path of profiles.toml (to edit it by hand)."""
    typer.echo(_path())


# --- run ---

def _cli(*args: str) -> List[str]:
    return [*colab_argv(), *args]


def _call(*args: str) -> int:
    """Run one of the CLI's subcommands in the foreground; its output is ours."""
    return subprocess.run(_cli(*args), stdin=subprocess.DEVNULL).returncode


def _runtime_flags(p: Profile) -> List[str]:
    flags = []
    if p.gpu:
        flags += ["--gpu", p.gpu]
    if p.tpu:
        flags += ["--tpu", p.tpu]
    if p.high_mem:
        flags.append("--high-mem")
    return flags


def _env_flags(p: Profile) -> List[str]:
    flags = []
    for s in p.secrets:
        flags += ["--secret", s]
    for k, v in p.env.items():
        flags += ["--env", f"{k}={v}"]
    return flags


def _alive(name: Optional[str]) -> bool:
    if not name:
        return False
    from colab_cli.common import state

    sessions, _ = state.sync_sessions()
    return name in sessions


def _remember(name: str, profile_name: str, p: Profile) -> None:
    """Mark the session as started by the profile (the tray reads this)."""
    from colab_cli.common import state

    s = state.store.get(name)
    if s is not None:
        s.profile = profile_name
        s.stop_when_done = p.stop_when_done
        state.store.add(s)


def _stop(name: str) -> None:
    typer.echo(f"[colab] Stopping runtime '{name}' (stop_when_done).", err=True)
    _call("stop", "-s", name)


def _wait_ready(url: str, proc, timeout: float) -> bool:
    """Poll ``url`` until it answers (any status below 500) or ``proc`` exits."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and proc.poll() is None:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(READY_POLL_SEC)
    return False


def _forward(name: str, p: Profile, no_open: bool) -> int:
    argv = ["forward", "-s", name]
    if p.ssh_key:
        argv += ["-i", os.path.expanduser(p.ssh_key)]
    argv += p.forward
    proc = subprocess.Popen(_cli(*argv), stdin=subprocess.DEVNULL)
    try:
        if p.ready_url:
            typer.echo(f"[colab] Waiting for {p.ready_url} ...", err=True)
            if _wait_ready(p.ready_url, proc, p.ready_timeout):
                typer.echo(f"[colab] Ready: {p.open or p.ready_url} (Ctrl+C closes the forward).", err=True)
                if p.open and not no_open:
                    webbrowser.open(p.open)
            elif proc.poll() is None:
                typer.echo(f"[colab] {p.ready_url} did not answer in {p.ready_timeout:.0f} s; the forward stays open.", err=True)
        return proc.wait()
    except KeyboardInterrupt:
        try:
            return proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            return 130


@profile_app.command("run")
def run(
    name: Annotated[str, typer.Argument(help="Profile name.")],
    foreground: Annotated[
        bool,
        typer.Option("--foreground", help="Job mode: run with `colab run` and wait (the runtime stops at the end)."),
    ] = False,
    no_open: Annotated[bool, typer.Option("--no-open", help="Do not open the profile's URL in a browser.")] = False,
):
    """Run a profile.

    Job mode starts the script as a background job and returns; the tray (or
    `colab job`) follows it, and with stop_when_done the tray stops the runtime
    once it ends. Kernel mode runs the script with `colab exec`, then keeps
    `colab forward` open (if the profile forwards ports) until Ctrl+C.
    """
    p = _get(name)
    script = resolve_script(p, _path())
    if not os.path.isfile(script):
        typer.echo(f"[colab] Profile '{name}': script not found: {script}", err=True)
        raise typer.Exit(2)
    reuse = _alive(p.session)
    session = p.session or f"{name}-{datetime.datetime.now():%m%d-%H%M%S}"
    if p.mode == "job":
        raise typer.Exit(_run_job(name, p, script, session, reuse, foreground))
    raise typer.Exit(_run_kernel(name, p, script, session, reuse, no_open))


def _run_job(name: str, p: Profile, script: str, session: str, reuse: bool, foreground: bool) -> int:
    budget = ["--budget", str(p.budget)] if p.budget is not None else []
    if reuse:
        argv = ["job", "start", "-s", session, *budget, *_env_flags(p), "-f", script]
        if p.args:
            argv += ["--", *p.args]
    else:
        detach = [] if foreground else ["--detach"]
        argv = ["run", *detach, "-s", session, *_runtime_flags(p), *budget, *_env_flags(p), script, *p.args]
    code = _call(*argv)
    if code == 0 and not foreground:
        _remember(session, name, p)
        if p.stop_when_done:
            typer.echo(
                f"[colab] stop_when_done: `colab tray` stops '{session}' once the job ends; "
                f"without the tray the runtime keeps running (`colab stop -s {session}`).",
                err=True,
            )
    return code


def _run_kernel(name: str, p: Profile, script: str, session: str, reuse: bool, no_open: bool) -> int:
    if reuse:
        typer.echo(f"[colab] Reusing runtime '{session}'.", err=True)
    else:
        code = _call("new", "-s", session, *_runtime_flags(p))
        if code != 0:
            return code
    _remember(session, name, p)
    argv = ["exec", "-s", session, "-f", script, *_env_flags(p)]
    if p.timeout is not None:
        argv += ["--timeout", str(p.timeout)]
    for var, value in p.vars.items():
        argv += ["--var", f"{var}={json.dumps(value)}"]
    code = _call(*argv)
    if code == 0 and p.forward:
        code = _forward(session, p, no_open)
    if p.stop_when_done:
        _stop(session)
    return code


def register(app: typer.Typer):
    app.add_typer(profile_app, name="profile")
