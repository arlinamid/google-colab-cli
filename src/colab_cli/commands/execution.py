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

import contextlib
import datetime
import nbformat
import os
import re
import sys
import tempfile
import time
import typer
from nbformat.v4 import new_output
from rich.console import Console
from typing import List, Optional
from typing_extensions import Annotated

from colab_cli.results import (
    SCHEMA,
    Block,
    JsonEmitter,
    build_cell_record,
    build_summary,
    failure_record,
    history_fields,
)
from colab_cli.cost import CostMeter
from colab_cli.runtime import ColabRuntime, RunLimitExceeded, SessionConnectionLost
from colab_cli.utils import (
    handle_image,
    is_terminal_error,
    read_python_source,
    render_display_data,
)
from colab_cli.secrets import resolve_secrets
from colab_cli.notebook import (
    TITLE_REGEX,  # noqa: F401  (re-exported; it used to live here)
    CellSelectionError,
    CodeCell,
    code_cells,
    preflight,
    select_cells,
)

_console = Console()

ENV_KEY_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SECRET_HELP = (
    "Set a secret in the remote kernel's environment without putting its "
    "value on the command line: NAME[=SOURCE], SOURCE is env (the local "
    "NAME variable, default), env:VAR, prompt, hf-cli or gh-cli. The value "
    "is written as *** in the history log. Repeat for multiple secrets."
)

def is_stdin_tty() -> bool:
    """True if stdin is a terminal."""
    return sys.stdin is not None and sys.stdin.isatty()


def is_stdout_tty() -> bool:
    """True if stdout is a terminal."""
    return sys.stdout is not None and sys.stdout.isatty()


def _parse_env_vars(env: Optional[List[str]]) -> dict[str, str]:
    """Parse repeatable --env KEY=VALUE entries into an ordered mapping."""
    env_vars = {}
    for item in env or []:
        if "=" not in item:
            typer.echo(
                f"[colab] Invalid --env value {item!r}. Expected KEY=VALUE.",
                err=True,
            )
            raise typer.Exit(2)

        key, value = item.split("=", 1)
        if not ENV_KEY_REGEX.fullmatch(key):
            typer.echo(
                f"[colab] Invalid --env key {key!r}. Expected a valid "
                "environment variable name.",
                err=True,
            )
            raise typer.Exit(2)

        env_vars[key] = value
    return env_vars


def _build_env_prelude(env_vars: dict[str, str]) -> str:
    """Build Python source that sets environment variables in the remote kernel."""
    if not env_vars:
        return ""

    lines = ["import os"]
    lines.extend(f"os.environ[{key!r}] = {value!r}" for key, value in env_vars.items())
    return "\n".join(lines) + "\n"


def save_output(outputs, cell):
    if cell is None:
        return

    if not hasattr(cell, "outputs"):
        cell.outputs = []
    else:
        cell.outputs.clear()

    for out in outputs:
        if out.get("output_type") == "stream":
            cell.outputs.append(
                new_output(
                    output_type="stream",
                    name=out.get("name", "stdout"),
                    text=out.get("text", ""),
                )
            )
        elif "data" in out:
            output_type = out.get("output_type", "display_data")
            cell.outputs.append(
                new_output(
                    output_type=output_type,
                    data=out["data"],
                    metadata=out.get("metadata", {}),
                )
            )
        elif out.get("output_type") == "error":
            cell.outputs.append(
                new_output(
                    output_type="error",
                    ename=out.get("ename", "Error"),
                    evalue=out.get("evalue", ""),
                    traceback=out.get("traceback", []),
                )
            )


def display_output(out, output_image=None):
    if out.get("output_type") == "stream":
        stream = sys.stderr if out.get("name") == "stderr" else sys.stdout
        stream.write(out.get("text", ""))
        stream.flush()
    elif "data" in out:
        data = out["data"]
        text = render_display_data(data)
        if text is not None:
            _console.print(text)
        if png := data.get("image/png"):
            handle_image(png, "image/png", target_path=output_image)
        elif jpeg := data.get("image/jpeg"):
            handle_image(jpeg, "image/jpeg", target_path=output_image)
    elif out.get("output_type") == "error":
        tb = out.get("traceback", [])
        if tb:
            sys.stderr.write("".join(tb) + "\n")
        else:
            ename = out.get("ename", "Error")
            evalue = out.get("evalue", "")
            sys.stderr.write(f"{ename}: {evalue}\n")
    else:
        # Ignore silent outputs like metadata or clear_output for streaming
        pass


def read_script(path: str) -> str:
    """Reads a local Python script as Python would (coding cookie, else UTF-8).

    Raises:
      typer.Exit: If it does not decode (exit code 2), with a hint.
    """
    try:
        return read_python_source(path)
    except (UnicodeDecodeError, SyntaxError) as e:
        typer.echo(
            f"[colab] Cannot read '{path}': {e}. Save it as UTF-8, or declare "
            "its encoding on the first or second line, e.g. "
            "'# -*- coding: cp932 -*-'.",
            err=True,
        )
        raise typer.Exit(2)


def _load_code_blocks(file: Optional[str], cells_spec: Optional[str]):
    """Read what `colab exec` should run. Returns (notebook or None, blocks).

    Each block is a dict with the code and a human-readable ``label``;
    notebook blocks also carry the cell, its number, title and id.
    """
    if file:
        if not os.path.isfile(file):
            typer.echo(f"[colab] File not found: '{file}'", err=True)
            raise typer.Exit(1)
        if not file.endswith(".ipynb"):
            code = read_script(file)
            return None, [{"code": code, "id": None, "label": file, "title": ""}]
        typer.echo(f"[colab] Parsing notebook '{file}'...")
        try:
            with open(file, "r", encoding="utf-8") as f:
                nb = nbformat.read(f, as_version=4)
            nbformat.validate(nb)
        except Exception as e:
            typer.echo(f"[colab] Cannot read notebook '{file}': {e}", err=True)
            raise typer.Exit(2)
        try:
            selected = select_cells(code_cells(nb), cells_spec)
        except CellSelectionError as e:
            typer.echo(f"[colab] {e}", err=True)
            raise typer.Exit(2)
        return nb, [
            {
                "code": c.source,
                "id": c.id,
                "cell": c.cell,
                "number": c.number,
                "title": c.title,
                "label": c.label,
            }
            for c in selected
        ]
    if is_stdin_tty():
        typer.echo("[colab] Error: No input provided. Pipe code or provide a file.")
        raise typer.Exit(1)
    return None, [{"code": sys.stdin.read(), "id": None, "label": "stdin", "title": ""}]


def _check_blocks(blocks) -> None:
    """The --check pre-flight: report every problem, run nothing if any."""
    problems = preflight(
        [
            CodeCell(
                number=b.get("number", 1),
                id=b.get("id"),
                title=b.get("title", ""),
                source=b["code"],
                name=None if "number" in b else b["label"],
            )
            for b in blocks
        ]
    )
    if problems:
        typer.echo(
            f"[colab] Check failed ({len(problems)} problem(s)); nothing was run:",
            err=True,
        )
        for problem in problems:
            typer.echo(f"  {problem}", err=True)
        raise typer.Exit(2)
    typer.echo(f"[colab] Check passed ({len(blocks)} cell(s)).")


def exec_command(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    file: Annotated[
        Optional[str], typer.Option("-f", "--file", help="File to execute")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option(
            "--timeout",
            help=(
                "Fail and interrupt the kernel after this many seconds without "
                "output. Default: no limit."
            ),
        ),
    ] = None,
    env: Annotated[
        Optional[List[str]],
        typer.Option(
            "--env",
            help=(
                "Set an environment variable in the remote kernel as KEY=VALUE. "
                "Repeat for multiple variables. For tokens use --secret."
            ),
        ),
    ] = None,
    secret: Annotated[
        Optional[List[str]],
        typer.Option("--secret", help=SECRET_HELP),
    ] = None,
    cells: Annotated[
        Optional[str],
        typer.Option(
            "--cells",
            help=(
                "Notebooks only: run just these code cells, in this order. "
                "Comma-separated code-cell numbers (from 1, markdown cells not "
                "counted), ranges such as 2-5, #@title values or cell ids. "
                "Entries that look like numbers or ranges are always read as "
                "such."
            ),
        ),
    ] = None,
    stop_on_error: Annotated[
        bool,
        typer.Option(
            "--stop-on-error",
            help="Notebooks only: stop at the first cell that raises an error.",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help=(
                "Before running anything, check the selected cells locally "
                "(notebook structure, --cells, Python syntax with IPython "
                "magics allowed). Nothing runs if a check fails."
            ),
        ),
    ] = False,
    check_only: Annotated[
        bool,
        typer.Option(
            "--check-only",
            help=(
                "Run the --check checks and print which cells would run, "
                "without a session and without executing anything."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            envvar="COLAB_CLI_JSON",
            help=(
                "For scripts and AI agents: print one JSON record per cell "
                "(output, error with its local file line, diagnosis and next "
                "steps), then a summary. Other output goes to stderr. Exit "
                "codes: 1 code error, 3 lost session, 4 timeout. Also set by "
                "COLAB_CLI_JSON=1."
            ),
        ),
    ] = False,
    json_stream: Annotated[
        bool,
        typer.Option(
            "--json-stream",
            help=(
                "Like --json, plus an output event for each piece of output "
                "as it arrives."
            ),
        ),
    ] = False,
    output_dir: Annotated[
        Optional[str],
        typer.Option(
            "--output-dir",
            help="With --json: where to save images (default: a new temp folder).",
        ),
    ] = None,
    budget: Annotated[
        Optional[float],
        typer.Option(
            "--budget",
            help=(
                "Stop (interrupt) the code once it has used about this many "
                "compute units, estimated from the account's hourly rate at "
                "the start. Exit code 5."
            ),
        ),
    ] = None,
):
    """Execute code in a session.

    Exits with code 1 if the code (or any notebook cell) raised an error.
    With --json, stdout carries only JSON records (docs/08_agent_feedback.md).
    """
    from colab_cli.common import state

    args = (state, session, file, output_image, timeout, env, secret, cells,
            stop_on_error, check, check_only)
    if not (json_output or json_stream):
        _exec(*args, budget=budget)
        return
    emitter = JsonEmitter(sys.stdout)
    # Human output ([colab] lines, notebook progress) must not mix with JSON.
    with contextlib.redirect_stdout(sys.stderr):
        _exec(*args, emitter=emitter, stream_events=json_stream,
              output_dir=output_dir, budget=budget)


def _exec(state, session, file, output_image, timeout, env, secret, cells,
          stop_on_error, check, check_only, *, emitter=None,
          stream_events=False, output_dir=None, budget=None):
    env_vars = _parse_env_vars(env)
    is_nb = bool(file and file.endswith(".ipynb"))
    if (cells is not None or stop_on_error) and not is_nb:
        typer.echo(
            "[colab] --cells and --stop-on-error only apply to .ipynb notebooks.",
            err=True,
        )
        raise typer.Exit(2)

    if not check_only:
        name = state.resolve_session(session)
        s = state.get_session(name)

    nb, code_blocks = _load_code_blocks(file, cells)

    if check or check_only:
        _check_blocks(code_blocks)
        if check_only:
            plan = ", ".join(b["label"] for b in code_blocks) or "nothing"
            typer.echo(f"[colab] Would run: {plan}")
            raise typer.Exit(0)

    if not any(b["code"].strip() for b in code_blocks):
        raise typer.Exit(0)

    env_vars.update(resolve_secrets(secret, state.history))

    def on_started(kid):
        s.kernel_id = kid
        state.store.add(s)

    def on_sess_started(sid):
        s.session_id = sid
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        session_name=s.name,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_started,
        on_session_started=on_sess_started,
    )
    try:
        # Ensure we are in /content which is the standard Colab working directory
        runtime.execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
        )
    except Exception as e:
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up."
            )
            state.prune_session(name)
            if emitter:
                rec = failure_record(
                    "session_lost", str(e), index=0, cell_id=None,
                    block=Block(label=file or "stdin"), session=name, duration_s=0,
                )
                emitter.emit(rec)
                emitter.emit(build_summary(
                    [rec], duration_s=0, session=name, exit_code=3, output_dir=None
                ))
                raise typer.Exit(3)
            raise typer.Exit(1)
        raise e

    failed = []
    records = []
    exit_code = 0
    started = time.monotonic()
    image_dir_used = None
    meter = None
    run_end = None
    if emitter or budget is not None:
        meter = CostMeter(state.client, accelerator=getattr(s, "accelerator", None))
    if budget is not None:
        seconds = meter.seconds_for(budget)
        if seconds is None:
            typer.echo(
                "[colab] --budget not enforced: no compute-unit rate is available.",
                err=True,
            )
        else:
            run_end = started + seconds

    def image_dir():
        nonlocal image_dir_used
        if image_dir_used is None:
            image_dir_used = output_dir or tempfile.mkdtemp(prefix="colab-out-")
        return image_dir_used

    try:
        s.running = f"exec({file or 'stdin'})"
        state.store.add(s)

        total = len(code_blocks)
        for i, block in enumerate(code_blocks):
            prelude = _build_env_prelude(env_vars)
            code = prelude + block["code"]
            identifier = None
            if is_nb:
                identifier = block["title"] or block.get("id") or ""
                identifier_str = f" - {identifier}" if identifier else ""
                position = f"{i + 1}/{total}"
                if cells is not None:
                    position = f"{block['number']} ({position})"
                typer.echo(f"[colab] Executing cell {position}{identifier_str}...")

            s.last_execution = (
                file or "stdin",
                identifier,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            s.activity = {
                "file": file or "stdin",
                "cell": i + 1,
                "total": total,
                "label": block["label"],
                "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            state.store.add(s)

            source = Block(
                label=block["label"],
                file=file or "<stdin>",
                cell=block.get("number") if is_nb else None,
                code=block["code"],
                prelude_lines=prelude.count("\n"),
            )
            block_started = time.monotonic()
            run_kwargs = {
                "output_hook": _output_hook(emitter, stream_events, i, output_image),
                "timeout": timeout,
            }
            if run_end is not None:
                run_kwargs["run_limit"] = max(run_end - block_started, 0.001)
            try:
                outputs = runtime.execute_code(code, **run_kwargs)
            except RunLimitExceeded as e:
                exit_code = 5
                if not emitter:
                    typer.echo(
                        f"[colab] Budget of {budget:g} compute units reached "
                        "(estimated); the kernel was interrupted.",
                        err=True,
                    )
                    break
                rec = failure_record(
                    "budget_reached", str(e), index=i, cell_id=block.get("id"),
                    block=source, session=name,
                    duration_s=time.monotonic() - block_started, budget=budget,
                )
                emitter.emit(rec)
                records.append(rec)
                break
            except (TimeoutError, SessionConnectionLost) as e:
                if not emitter:
                    raise
                kind = "idle_timeout" if isinstance(e, TimeoutError) else "session_lost"
                rec = failure_record(
                    kind, str(e) or kind, index=i, cell_id=block.get("id"),
                    block=source, session=name,
                    duration_s=time.monotonic() - block_started, timeout=timeout,
                )
                emitter.emit(rec)
                records.append(rec)
                exit_code = 4 if kind == "idle_timeout" else 3
                break
            if "cell" in block:
                save_output(outputs, block["cell"])
            block_duration = time.monotonic() - block_started
            event = {
                "code": code,
                "outputs": outputs,
                "cell_index": (block["number"] - 1 if is_nb else None),
                "cell_id": block.get("id"),
                **history_fields(outputs, block=source, duration_s=block_duration),
            }
            if emitter:
                rec = build_cell_record(
                    outputs, index=i, cell_id=block.get("id"), block=source,
                    duration_s=block_duration, session=name,
                    image_dir=image_dir,
                )
                add_seen_before(rec, state.history)
                emitter.emit(rec)
                records.append(rec)
            state.history.log_event(name, "execution", event)
            if any(o.get("output_type") == "error" for o in outputs or []):
                failed.append(block["label"])
                if stop_on_error and i + 1 < total:
                    typer.echo(
                        f"[colab] Stopping at {block['label']} (--stop-on-error); "
                        f"{total - i - 1} cell(s) not run.",
                        err=True,
                    )
                    break
    finally:
        s.running = None
        s.activity = None
        state.store.update_if_present(s)
        runtime.stop()
        if is_nb:
            output_file = os.path.splitext(file)[0] + "_output.ipynb"
            typer.echo(f"[colab] Saving notebook with outputs to '{output_file}'...")
            with open(output_file, "w", encoding="utf-8") as f:
                nbformat.write(nb, f)

    if failed and is_nb:
        typer.echo(
            f"[colab] {len(failed)} cell(s) raised an error: {', '.join(failed)}",
            err=True,
        )
    if emitter:
        exit_code = exit_code or (1 if failed else 0)
        duration = time.monotonic() - started
        emitter.emit(build_summary(
            records, duration_s=duration, session=name,
            exit_code=exit_code, output_dir=image_dir_used,
            cost=meter.block(duration) if meter else None,
        ))
        if exit_code:
            raise typer.Exit(exit_code)
        return
    if exit_code:
        raise typer.Exit(exit_code)
    if failed:
        raise typer.Exit(1)


def add_seen_before(rec, history) -> None:
    """Phase E: say whether this failure happened before and what fixed it.

    Reads the local history; any problem with it leaves seen_before null
    rather than failing the run.
    """
    diagnosis = rec.get("diagnosis")
    if not diagnosis:
        return
    diagnosis["seen_before"] = None
    error = rec.get("error") or {}
    try:
        from colab_cli.experience import load_events, seen_before

        diagnosis["seen_before"] = seen_before(
            load_events(history),
            {"diagnosis": diagnosis["code"], "ename": error.get("ename"),
             "evalue": error.get("evalue")},
        )
    except Exception:
        pass


def _output_hook(emitter, stream_events, index, output_image):
    """Show output as it arrives, or with --json-stream emit it as events."""
    if emitter is None:
        return lambda o: display_output(o, output_image)

    def hook(o):
        if stream_events and "text" in o and o.get("output_type", "stream") == "stream":
            emitter.emit({
                "schema": SCHEMA,
                "type": "output",
                "cell_index": index,
                "stream": "stderr" if o.get("name") == "stderr" else "stdout",
                "text": o["text"],
            })

    return hook

def repl(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
):
    """Start an interactive REPL"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.get_session(name)

    def on_started(kid):
        s.kernel_id = kid
        state.store.add(s)

    def on_sess_started(sid):
        s.session_id = sid
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        session_name=s.name,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_started,
        on_session_started=on_sess_started,
    )
    try:
        # Ensure we are in /content which is the standard Colab working directory
        runtime.execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
        )
    except Exception as e:
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up."
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e

    if not is_stdin_tty():
        code = sys.stdin.read()
        if not code.strip():
            raise typer.Exit(0)

        s.last_execution = (
            "stdin",
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        s.running = "repl(stdin)"
        state.store.add(s)
        try:
            outputs = runtime.execute_code(
                code, output_hook=lambda o: display_output(o, output_image)
            )
            state.history.log_event(
                name, "execution", {"code": code, "outputs": outputs, "source": "piped"}
            )
        finally:
            s.running = None
            state.store.update_if_present(s)
            runtime.stop()
    else:
        from colab_cli.repl import ColabREPL

        s.running = "repl"
        state.store.add(s)
        try:
            repl_inst = ColabREPL(
                runtime,
                session_name=s.name,
                history_logger=state.history,
                output_image=output_image,
            )
            state.history.log_event(name, "repl_started", {})
            repl_inst.run()
        finally:
            s.running = None
            state.store.update_if_present(s)


def register(app: typer.Typer):
    app.command(name="exec")(exec_command)
    app.command()(repl)
