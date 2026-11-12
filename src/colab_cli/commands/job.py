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

"""`colab job`: long runs in the background on the runtime (docs/08, phase B).

`start` hands the job to a runner process on the VM and exits; `status`,
`tail`, `wait`, `stop` and `list` read the job's files over the Contents API.
Nothing keeps running on this machine (fire-and-forget); only `wait` stays in
the foreground, pinging keep-alive between polls.
"""

import json
import os
import sys
import time
from typing import List, Optional

import typer
from typing_extensions import Annotated

from colab_cli.commands.execution import SECRET_HELP, _parse_env_vars, read_script
from colab_cli.contents import ContentsClient
from colab_cli.jobs import (
    DEFAULT_STALL_AFTER,
    JOBS_DIR,
    RUNNING_STATES,
    build_start_code,
    job_argv,
    job_record,
    new_job_id,
    new_lines,
    parse_metrics,
)
from colab_cli.keepalive import _Pinger
from colab_cli.results import JsonEmitter
from colab_cli.runtime import ColabRuntime
from colab_cli.secrets import resolve_secrets

job_app = typer.Typer(
    help=(
        "Run long jobs in the background on the runtime and follow them "
        "(progress, metrics, stalls, failures)."
    ),
    no_args_is_help=True,
)

SessionOpt = Annotated[Optional[str], typer.Option("-s", "--session", help="Session name")]
JsonOpt = Annotated[bool, typer.Option("--json", envvar="COLAB_CLI_JSON", help="Print JSON records.")]
StallOpt = Annotated[
    float,
    typer.Option("--stall-after", help="Call a running job stalled after this many seconds without output."),
]


def _session(session):
    from colab_cli.common import state

    name = state.resolve_session(session)
    return state, name, state.get_session(name)


def _job_ids(files) -> List[str]:
    try:
        listing = files.list_dir(JOBS_DIR)
    except FileNotFoundError:
        return []
    ids = [i["name"] for i in listing.get("content", []) if i.get("type") == "directory"]
    # Ids are <name>-YYYYMMDD-HHMMSS-xxxx: oldest first by their time stamp.
    return sorted(ids, key=lambda i: (i.split("-")[-3:-1], i))


def _resolve_job(files, job_id: Optional[str], session: str) -> str:
    if job_id:
        return job_id
    ids = _job_ids(files)
    if not ids:
        typer.echo(f"[colab] No jobs on session '{session}'.", err=True)
        raise typer.Exit(1)
    return ids[-1]


def _read_status(files, job_id: str, session: str) -> dict:
    try:
        return json.loads(files.read_text(f"{JOBS_DIR}/{job_id}/status.json"))
    except FileNotFoundError:
        typer.echo(f"[colab] No job '{job_id}' on session '{session}' (see `colab job list -s {session}`).", err=True)
        raise typer.Exit(1)


def _read_optional(files, path: str) -> str:
    try:
        return files.read_text(path)
    except FileNotFoundError:
        return ""


def _record(files, job_id, session, stall_after, tail_lines=20):
    status = _read_status(files, job_id, session)
    base = f"{JOBS_DIR}/{job_id}"
    return job_record(
        status,
        metrics=parse_metrics(_read_optional(files, f"{base}/metrics.jsonl")),
        tail=_read_optional(files, f"{base}/tail.txt"),
        now=time.time(),
        stall_after=stall_after,
        session=session,
        tail_lines=tail_lines,
    )


def _duration(seconds) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


def _progress_text(progress) -> str:
    if not progress:
        return ""
    text = f"{progress['percent']}%"
    if progress.get("total"):
        text += f" ({progress['n']}/{progress['total']})"
    if progress.get("eta_s") is not None:
        text += f", eta {_duration(progress['eta_s'])}"
    return text


def _print_human(rec, show_tail=True) -> None:
    head = f"{rec['id']}: {rec['state']}"
    if rec["exit_code"] is not None:
        head += f" (exit {rec['exit_code']})"
    head += f", elapsed {_duration(rec['elapsed_s'])}"
    if rec["silent_s"] is not None:
        head += f", last output {_duration(rec['silent_s'])} ago"
    typer.echo(head)
    if rec["progress"]:
        typer.echo(f"  progress: {_progress_text(rec['progress'])}")
    if rec["metrics"]["last"]:
        typer.echo("  metrics:  " + " ".join(f"{k}={v}" for k, v in rec["metrics"]["last"].items()))
    if show_tail and rec["tail"]:
        typer.echo("  last output:")
        for line in rec["tail"]:
            typer.echo(f"    {line}")
    if rec["diagnosis"]:
        typer.echo(f"  diagnosis: {rec['diagnosis']['summary']}")
        for action in rec["diagnosis"]["next_actions"]:
            typer.echo(f"    - {action['cmd'] or action['why']}" + (f"  ({action['why']})" if action["cmd"] else ""))


@job_app.command(
    "start",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def start(
    session: SessionOpt = None,
    file: Annotated[Optional[str], typer.Option("-f", "--file", help="Local Python script to run as the job.")] = None,
    name: Annotated[Optional[str], typer.Option("--name", help="Name at the start of the job id.")] = None,
    env: Annotated[
        Optional[List[str]],
        typer.Option("--env", help="KEY=VALUE environment variable for the job. Repeatable."),
    ] = None,
    secret: Annotated[Optional[List[str]], typer.Option("--secret", help=SECRET_HELP)] = None,
    json_output: JsonOpt = False,
    cmd: Annotated[
        Optional[List[str]],
        typer.Argument(
            help=(
                "With -f: arguments for the script. Without -f: the command to "
                "run (one argument runs in bash, several are an exact argv). "
                "Put them after --."
            ),
        ),
    ] = None,
):
    """Start a job in the background and return at once.

    The job runs as its own process on the runtime, apart from the kernel, so
    `colab exec` stays usable and the job survives this command exiting. In a
    Python job, `from colab_feedback import report, note` records metrics and
    notes that `colab job status` shows.
    """
    if not file and not cmd:
        typer.echo("[colab] Give a script with -f FILE, or a command after --.", err=True)
        raise typer.Exit(2)
    if file and not os.path.isfile(file):
        typer.echo(f"[colab] File not found: '{file}'", err=True)
        raise typer.Exit(2)
    source = read_script(file) if file else None
    argv = job_argv(file, None if file else cmd, (cmd or []) if file else [])
    env_vars = _parse_env_vars(env)
    state, session_name, s = _session(session)
    env_vars.update(resolve_secrets(secret, state.history))
    label = name or (os.path.splitext(os.path.basename(file))[0] if file else os.path.basename(argv[0]))
    job_id = new_job_id(label)

    runtime = ColabRuntime(
        s.url, s.token, session_name=s.name, kernel_id=s.kernel_id, session_id=s.session_id,
    )
    try:
        outputs = runtime.execute_code(
            build_start_code(job_id, name=label, argv=argv, env=env_vars, script_source=source)
        )
    finally:
        runtime.stop()
    errors = [o for o in outputs or [] if o.get("output_type") == "error"]
    info = None
    for o in outputs or []:
        for line in o.get("text", "").splitlines():
            if line.startswith("COLAB_JOB "):
                info = json.loads(line[len("COLAB_JOB "):])
    if errors or not info:
        detail = f"{errors[-1].get('ename')}: {errors[-1].get('evalue')}" if errors else "no reply"
        typer.echo(f"[colab] Could not start the job ({detail}).", err=True)
        raise typer.Exit(1)

    state.history.log_event(session_name, "job_started", {"id": job_id, "argv": argv, "file": file})
    if json_output:
        now = time.time()
        rec = job_record(
            {"id": job_id, "name": label, "argv": argv, "state": "starting", "pid": info["pid"],
             "started_at": now, "ended_at": None, "exit_code": None, "last_output_at": None, "progress": None},
            metrics=[], tail="", now=now, stall_after=DEFAULT_STALL_AFTER, session=session_name,
        )
        JsonEmitter(sys.stdout).emit(rec)
        return
    typer.echo(f"[colab] Started job {job_id} (pid {info['pid']}) on '{session_name}'.")
    typer.echo(
        f"[colab] Follow it with `colab job status -s {session_name} {job_id}`, "
        f"`colab job tail -s {session_name} {job_id} --follow` or "
        f"`colab job wait -s {session_name} {job_id}`."
    )


@job_app.command("status")
def status(
    job_id: Annotated[Optional[str], typer.Argument(help="Job id (default: the newest job).")] = None,
    session: SessionOpt = None,
    json_output: JsonOpt = False,
    stall_after: StallOpt = DEFAULT_STALL_AFTER,
    lines: Annotated[int, typer.Option("-n", "--lines", help="Lines of output to show.")] = 20,
):
    """Show a job's state, progress, metrics, last output and diagnosis."""
    _state, session_name, s = _session(session)
    files = ContentsClient(s)
    job_id = _resolve_job(files, job_id, session_name)
    rec = _record(files, job_id, session_name, stall_after, tail_lines=lines)
    if json_output:
        JsonEmitter(sys.stdout).emit(rec)
    else:
        _print_human(rec)


@job_app.command("tail")
def tail(
    job_id: Annotated[Optional[str], typer.Argument(help="Job id (default: the newest job).")] = None,
    session: SessionOpt = None,
    lines: Annotated[int, typer.Option("-n", "--lines", help="Lines to show (at most 200).")] = 20,
    follow: Annotated[bool, typer.Option("--follow", "-F", help="Keep printing new output until the job ends.")] = False,
    interval: Annotated[float, typer.Option("--interval", help="Seconds between checks with --follow.")] = 5.0,
):
    """Print a job's last output lines (the full log: `colab download`)."""
    _state, session_name, s = _session(session)
    files = ContentsClient(s)
    job_id = _resolve_job(files, job_id, session_name)
    _read_status(files, job_id, session_name)
    path = f"{JOBS_DIR}/{job_id}/tail.txt"
    shown = _read_optional(files, path).splitlines()
    for line in shown[-lines:]:
        typer.echo(line)
    while follow:
        if _read_status(files, job_id, session_name).get("state") not in RUNNING_STATES:
            current = _read_optional(files, path).splitlines()
            for line in new_lines(shown, current):
                typer.echo(line)
            break
        time.sleep(interval)
        current = _read_optional(files, path).splitlines()
        for line in new_lines(shown, current):
            typer.echo(line)
        shown = current


@job_app.command("wait")
def wait(
    job_id: Annotated[Optional[str], typer.Argument(help="Job id (default: the newest job).")] = None,
    session: SessionOpt = None,
    interval: Annotated[float, typer.Option("--interval", help="Seconds between checks.")] = 30.0,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Give up after this many seconds (exit 4); the job keeps running."),
    ] = None,
    json_output: JsonOpt = False,
    stall_after: StallOpt = DEFAULT_STALL_AFTER,
):
    """Wait for a job to end, keeping the runtime alive; exit with its code.

    Exit codes: the job's own exit code (0 on success), 130 if it was
    stopped, 4 if --timeout passed first.
    """
    state, session_name, s = _session(session)
    files = ContentsClient(s)
    job_id = _resolve_job(files, job_id, session_name)
    pinger = _Pinger(state.client, s.endpoint)
    started = time.monotonic()
    while True:
        rec = _record(files, job_id, session_name, stall_after)
        if rec["state"] not in RUNNING_STATES:
            break
        if timeout is not None and time.monotonic() - started >= timeout:
            if json_output:
                JsonEmitter(sys.stdout).emit(rec)
            typer.echo(f"[colab] Job {job_id} is still running after {_duration(timeout)}.", err=True)
            raise typer.Exit(4)
        line = f"[colab] {job_id}: {rec['state']}"
        if rec["progress"]:
            line += f" {_progress_text(rec['progress'])}"
        if rec["stalled"]:
            line += f" (no output for {_duration(rec['silent_s'])})"
        typer.echo(line, err=True)
        pinger.ping()
        time.sleep(interval)
    if json_output:
        JsonEmitter(sys.stdout).emit(rec)
    else:
        _print_human(rec)
    if rec["state"] == "stopped":
        raise typer.Exit(130)
    code = rec["exit_code"]
    if rec["state"] == "failed":
        raise typer.Exit(code if isinstance(code, int) and code > 0 else 1)


@job_app.command("stop")
def stop(
    job_id: Annotated[str, typer.Argument(help="Job id.")],
    session: SessionOpt = None,
):
    """Ask a job to stop (it gets SIGTERM, then SIGKILL after 10 s)."""
    _state, session_name, s = _session(session)
    files = ContentsClient(s)
    status_ = _read_status(files, job_id, session_name)
    if status_.get("state") not in RUNNING_STATES:
        typer.echo(f"[colab] Job {job_id} already ended ({status_.get('state')}).")
        return
    files.write_text(f"{JOBS_DIR}/{job_id}/stop", "")
    typer.echo(f"[colab] Asked job {job_id} to stop; it ends within about 10 seconds.")


@job_app.command("list")
def list_jobs(
    session: SessionOpt = None,
    json_output: JsonOpt = False,
    stall_after: StallOpt = DEFAULT_STALL_AFTER,
):
    """List the jobs on a session, oldest first."""
    _state, session_name, s = _session(session)
    files = ContentsClient(s)
    ids = _job_ids(files)
    if not ids:
        if not json_output:
            typer.echo(f"[colab] No jobs on session '{session_name}'.")
        return
    emitter = JsonEmitter(sys.stdout) if json_output else None
    for job_id in ids:
        try:
            status_ = json.loads(files.read_text(f"{JOBS_DIR}/{job_id}/status.json"))
        except (FileNotFoundError, ValueError):
            continue
        rec = job_record(status_, metrics=[], tail=None, now=time.time(),
                         stall_after=stall_after, session=session_name)
        if emitter:
            emitter.emit(rec)
        else:
            extra = _progress_text(rec["progress"]) if rec["state"] in RUNNING_STATES else (
                f"exit {rec['exit_code']}" if rec["exit_code"] is not None else ""
            )
            typer.echo(f"{job_id}  {rec['state']:<9}  {_duration(rec['elapsed_s']):>10}  {extra}".rstrip())


def register(app: typer.Typer):
    app.add_typer(job_app, name="job")
