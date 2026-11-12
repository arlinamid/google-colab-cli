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

"""
`colab run <script.py> [args...]` — shebang-friendly one-shot execution.

Combines `colab new` + `colab exec` + `colab stop` into a single fire-and-forget
invocation. The Python script's body runs in a freshly-allocated Colab kernel
with `sys.argv` set as if it had been invoked via `python script.py [args...]`,
and the VM is automatically released when the script finishes (unless `--keep`
is passed).

Designed to support shebangs:

    #!/usr/bin/env -S colab run --gpu T4
    import torch
    print(torch.cuda.get_device_name(0))

See docs/05_run_command.md for the full design.
"""

import datetime
import os
import sys
import tempfile
import time
import uuid
from typing import List, Optional

import typer
from typing_extensions import Annotated

from colab_cli.client import (
    Accelerator,
    ColabRequestError,
    HIGH_MEM_ONLY_ACCELERATORS,
    PostAssignmentResponse,
    Shape,
    TooManyAssignmentsError,
)
from colab_cli.commands.execution import (
    add_seen_before,
    SECRET_HELP,
    _build_env_prelude,
    _parse_env_vars,
    read_script,
)
from colab_cli.commands.session import (
    GPU_ACCELERATORS,
    TPU_ACCELERATORS,
    resolve_runtime_options,
)
from colab_cli.secrets import resolve_secrets
from colab_cli.results import (
    Block,
    JsonEmitter,
    build_cell_record,
    build_summary,
    failure_record,
    history_fields,
)
from colab_cli.cost import CostMeter
from colab_cli.runtime import ColabRuntime, RunLimitExceeded, SessionConnectionLost
from colab_cli.state import SessionState
from colab_cli.utils import get_status_code, is_terminal_error, undo_msys_args


def _build_script_payload(
    script_path: str, script_args: List[str], env_vars: Optional[dict[str, str]] = None
) -> str:
    """Wrap the script body so it executes with native-`python`-like semantics.

    Specifically:
      - `sys.argv = [<basename>, *script_args]` so `argparse` etc. work.
      - `__name__ = '__main__'` so `if __name__ == "__main__":` guards fire.
      - Requested `--env KEY=VALUE` pairs are written into `os.environ`.
      - Suppress the IPython UserWarning "To exit: use 'exit', 'quit', or
        Ctrl-D." which fires whenever the script calls `sys.exit(...)`. This
        warning is meaningful in an interactive REPL, but for `colab run` it
        is pure noise that doesn't appear when running `python script.py`.

    The script body is appended verbatim; the prelude is short so any
    traceback line numbers from user code remain close to the original.
    """
    basename = os.path.basename(script_path)
    body = read_script(script_path)

    # `repr()` produces a safe, round-trippable Python literal for arbitrary
    # strings (handles quotes, backslashes, non-ASCII).
    argv_literal = f"[{', '.join(repr(x) for x in [basename] + script_args)}]"

    return (
        "import sys, warnings\n"
        f"sys.argv = {argv_literal}\n"
        "__name__ = '__main__'\n"
        "warnings.filterwarnings('ignore', message=\"To exit: use\")\n"
        + _build_env_prelude(env_vars or {})
        + _strip_shebang(body)
    )


def _extract_env_args_from_script_args(
    script_args: List[str],
) -> tuple[List[str], List[str]]:
    """Pull colab's --env options out of variadic script args.

    `colab run` intentionally forwards unknown options after the script path to
    the user's script. Since `--env` is now a colab option, support both
    `colab run --env KEY=VALUE script.py` (Typer parses this) and
    `colab run script.py --env KEY=VALUE` (this helper parses it).
    """
    forwarded_args = []
    env = []
    i = 0
    while i < len(script_args):
        arg = script_args[i]
        if arg == "--env":
            if i + 1 >= len(script_args):
                env.append("")
                i += 1
            else:
                env.append(script_args[i + 1])
                i += 2
        elif arg.startswith("--env="):
            env.append(arg.split("=", 1)[1])
            i += 1
        else:
            forwarded_args.append(arg)
            i += 1
    return forwarded_args, env


def _strip_shebang(body: str) -> str:
    """Remove a leading `#!...\\n` if present. The remote kernel doesn't need
    or understand it (it's a contract between the local kernel and the file's
    executable bit), and leaving it in just adds noise.
    """
    if body.startswith("#!"):
        nl = body.find("\n")
        return body[nl + 1 :] if nl != -1 else ""
    return body


def _is_systemexit(out) -> bool:
    """True iff this output is a `raise SystemExit(...)` (a.k.a. `sys.exit`)."""
    return out.get("output_type") == "error" and out.get("ename") == "SystemExit"


def _systemexit_code(out) -> int:
    """Map a SystemExit kernel output back to a CPython-style integer exit code.

    CPython conventions (mirrored):
      - `sys.exit()` / `sys.exit(None)` / `sys.exit(0)` -> 0
      - `sys.exit(<int>)`                                -> <int>
      - `sys.exit('msg')` (any non-int)                  -> 1
    """
    evalue = (out.get("evalue") or "").strip()
    if evalue in ("", "None", "0"):
        return 0
    try:
        return int(evalue)
    except ValueError:
        return 1


def _exit_code_from_outputs(outputs) -> int:
    """Derive the CLI's exit code from the kernel's outputs for a single cell.

    A `SystemExit` is treated like CPython would treat the same call from a
    plain `python script.py` invocation. Any *other* error (uncaught
    exception, NameError, etc.) is exit 1.
    """
    code = 0
    for o in outputs:
        if o.get("output_type") != "error":
            continue
        if _is_systemexit(o):
            ec = _systemexit_code(o)
            # Last SystemExit wins, matching the runtime — and any non-zero
            # eclipses any prior zero.
            code = ec if ec != 0 else code
        else:
            return 1
    return code


def _make_run_output_hook(output_image=None):
    """Build an `output_hook` for `runtime.execute_code` that:
      - Routes normal output to `display_output` (stream/image/error).
      - Suppresses the `SystemExit` traceback so `sys.exit(0)` is silent (it
        wouldn't print anything under `python script.py` either) and
        `sys.exit(N)` doesn't dump a noisy IPython-styled traceback when the
        intent is "shell exit code N".

    The kernel still RETURNS the SystemExit output to us (so we can derive the
    exit code in `_exit_code_from_outputs`); we just don't render it.
    """
    # Imported here to avoid a circular import via execution.py at module load.
    from colab_cli.commands.execution import display_output

    def hook(out):
        if _is_systemexit(out):
            return
        display_output(out, output_image)

    return hook


def run_command(
    ctx: typer.Context,
    script: Annotated[
        str,
        typer.Argument(
            help="Path to a local Python file to execute on a fresh Colab VM."
        ),
    ],
    script_args: Annotated[
        Optional[List[str]],
        typer.Argument(
            help=(
                "Arguments forwarded to the script as sys.argv[1:]. "
                "Anything after the script path is passed through verbatim."
            ),
        ),
    ] = None,
    session: Annotated[
        Optional[str],
        typer.Option(
            "-s",
            "--session",
            help=(
                "Name for the ephemeral session (auto-generated if omitted). "
                "Useful with --keep so you can attach later via `colab exec -s <name>`."
            ),
        ),
    ] = None,
    tpu: Annotated[
        Optional[str],
        typer.Option(
            help=(
                "TPU accelerator variant. Supported: "
                f"{', '.join(name.upper() for name in TPU_ACCELERATORS)}."
            )
        ),
    ] = None,
    gpu: Annotated[
        Optional[str],
        typer.Option(
            help=(
                "GPU accelerator variant. Supported: "
                f"{', '.join(name.upper() for name in GPU_ACCELERATORS)}. "
                "If omitted (along with --tpu), a CPU runtime is created."
            ),
        ),
    ] = None,
    high_mem: Annotated[
        bool,
        typer.Option(
            "--high-mem",
            help=(
                "Request a high-RAM machine shape. Requires Colab Pro or Pro+ "
                "entitlement. Ignored for L4 and TPU accelerators."
            ),
        ),
    ] = False,
    keep: Annotated[
        bool,
        typer.Option(
            "--keep",
            help=(
                "Do not stop the session after the script finishes. The session "
                "remains in `colab sessions` until you run `colab stop`."
            ),
        ),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach",
            help=(
                "Start the script as a background job (`colab job`) on the new "
                "runtime and return at once; the runtime keeps running until "
                "you `colab stop` it. --budget then stops the job on the runtime."
            ),
        ),
    ] = False,
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
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            envvar="COLAB_CLI_JSON",
            help=(
                "For scripts and AI agents: print a JSON record for the "
                "script (output, error with its file line, diagnosis and "
                "next steps) and a summary; the script's output goes into "
                "the record. Put it before the script path. The exit code "
                "stays the script's; 3 lost session, 4 timeout. Also set by "
                "COLAB_CLI_JSON=1."
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
                "Stop the script once it has used about this many compute units, "
                "estimated from the account's hourly rate at the start. Put it "
                "before the script path. Exit code 5."
            ),
        ),
    ] = None,
):
    """Run a Python script on a fresh Colab VM, then release the VM

    Designed to be used as a shebang interpreter, e.g.

        #!/usr/bin/env -S colab run --gpu T4

    so a single executable .py file can rent a GPU, run, and clean up after
    itself.
    """
    from colab_cli.common import state

    # The script runs on the runtime: its path arguments are remote paths.
    script_args = undo_msys_args(script_args or [])
    script_args, inline_env = _extract_env_args_from_script_args(script_args)
    env_vars = _parse_env_vars([*(env or []), *inline_env])
    env_vars.update(resolve_secrets(secret, state.history))

    # AGENTS.md item 10: validate locally BEFORE allocating a VM. A typo'd
    # script path should not cost the user real compute.
    if not os.path.isfile(script):
        typer.echo(f"[colab] Script not found: {script}", err=True)
        raise typer.Exit(2)
    # An undecodable script must fail before a VM is allocated.
    read_script(script)

    name = session or f"run-{uuid.uuid4().hex[:6]}"
    variant, accelerator, shape = resolve_runtime_options(
        gpu, tpu, high_mem=high_mem
    )

    if high_mem and accelerator in HIGH_MEM_ONLY_ACCELERATORS:
        typer.echo(
            "[colab] --high-mem ignored: this accelerator only offers one "
            "machine shape.",
            err=True,
        )

    typer.echo(f"[colab] Creating session '{name}'...", err=True)
    try:
        res = state.client.assign(
            uuid.uuid4(), variant=variant, accelerator=accelerator, shape=shape
        )
    except TooManyAssignmentsError:
        # Mirror `colab new`'s friendly precondition-failed message.
        typer.echo(
            "[colab] Allocation refused (precondition failed). This can mean "
            "too many active sessions, or a temporary usage or capacity "
            "limit for the requested runtime. Run `colab stop` to free up a "
            "session, wait and retry, or try a different accelerator.",
            err=True,
        )
        raise typer.Exit(code=1)
    except ColabRequestError as e:
        # Mirror `colab new`'s friendly accelerator-quota message.
        if get_status_code(e) == 400 and accelerator != Accelerator.NONE:
            typer.echo(
                f"[colab] Backend rejected accelerator '{accelerator.value}'. "
                "You may not have quota or entitlement for this accelerator on "
                "your account. Try a different one (e.g. --gpu T4) or omit "
                "--gpu/--tpu for a CPU runtime.",
                err=True,
            )
            raise typer.Exit(code=1)
        raise

    if isinstance(res, PostAssignmentResponse):
        token = res.runtime_proxy_info.token
        url = res.runtime_proxy_info.url
        endpoint = res.endpoint
    else:
        token = (
            res.runtime_proxy_info.token
            if hasattr(res, "runtime_proxy_info")
            else getattr(res, "runtime_proxy_token", "")
        )
        url = res.runtime_proxy_info.url if hasattr(res, "runtime_proxy_info") else ""
        endpoint = res.endpoint

    s = SessionState(
        name=name,
        token=token,
        url=url,
        endpoint=endpoint,
        token_expires_at=(
            res.runtime_proxy_info.expires_at()
            if hasattr(res, "runtime_proxy_info")
            else None
        ),
        variant=variant.value,
        accelerator=accelerator.value,
        machine_shape=(
            Shape.HIGH_RAM.name if shape == Shape.HIGH_RAM else Shape.STANDARD.name
        ),
    )

    state.store.add(s)
    state.history.log_event(
        name,
        "session_created",
        {
            "endpoint": endpoint,
            "variant": variant.value,
            "accelerator": accelerator.value,
            "machine_shape": s.machine_shape,
            "via": "run",
        },
    )
    if detach:
        from colab_cli.commands.job import launch_job
        from colab_cli.jobs import job_argv

        typer.echo(f"[colab] Session READY ({name}). Starting {script} as a job...", err=True)
        launch_job(
            state, name, s, file=script, source=read_script(script),
            argv=job_argv(script, None, script_args), env_vars=env_vars,
            name=None, budget=budget, json_output=json_output,
        )
        typer.echo(
            f"[colab] The runtime keeps running after the job; stop it with "
            f"`colab stop -s {name}`.",
            err=json_output,
        )
        return
    typer.echo(f"[colab] Session READY ({name}). Executing {script}...", err=True)

    # ----- Execute the script -------------------------------------------------
    exit_code = 0
    cleanup_reason = "run_completed"
    emitter = JsonEmitter(sys.stdout) if json_output else None
    records = []
    started = time.monotonic()
    # Next steps can name the session only if it still exists afterwards.
    live_session = name if keep else None
    image_dir_used = None

    def image_dir():
        nonlocal image_dir_used
        if image_dir_used is None:
            image_dir_used = output_dir or tempfile.mkdtemp(prefix="colab-out-")
        return image_dir_used

    meter = None
    run_limit = None
    if json_output or budget is not None:
        meter = CostMeter(state.client, accelerator=accelerator.value)
    if budget is not None:
        run_limit = meter.seconds_for(budget)
        if run_limit is None:
            typer.echo(
                "[colab] --budget not enforced: no compute-unit rate is available.",
                err=True,
            )

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
        # Same /content prelude as `colab exec` for consistency.
        try:
            runtime.execute_code(
                "import os; os.makedirs('/content', exist_ok=True); "
                "os.chdir('/content')"
            )
        except Exception as e:
            if is_terminal_error(e):
                typer.echo(
                    f"[colab] Session '{name}' appears to be lost (404/401).",
                    err=True,
                )
                state.prune_session(name)
                if emitter:
                    records.append(failure_record(
                        "session_lost", str(e), index=0, cell_id=None,
                        block=Block(label=script, file=script), session=None,
                        duration_s=0,
                    ))
                    emitter.emit(records[-1])
                    emitter.emit(build_summary(
                        records, duration_s=time.monotonic() - started,
                        session=name, exit_code=3, output_dir=None,
                    ))
                    raise typer.Exit(3)
                raise typer.Exit(1)
            raise

        payload = _build_script_payload(script, script_args, env_vars)
        s.running = f"run({os.path.basename(script)})"
        s.last_execution = (
            script,
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        state.store.add(s)

        source = _script_block(script, payload)
        run_started = time.monotonic()
        run_kwargs = {
            # With --json the script's output goes into the record only.
            "output_hook": (lambda o: None) if emitter else _make_run_output_hook(),
            "timeout": timeout,
        }
        if run_limit is not None:
            run_kwargs["run_limit"] = max(run_limit - (time.monotonic() - started), 0.001)
        try:
            outputs = runtime.execute_code(payload, **run_kwargs)
        except RunLimitExceeded as e:
            exit_code = 5
            cleanup_reason = "run_failed"
            if emitter:
                records.append(failure_record(
                    "budget_reached", str(e), index=0, cell_id=None, block=source,
                    session=live_session, duration_s=time.monotonic() - run_started,
                    budget=budget,
                ))
                emitter.emit(records[-1])
            else:
                typer.echo(
                    f"[colab] Budget of {budget:g} compute units reached "
                    "(estimated); the script was interrupted.",
                    err=True,
                )
        except (TimeoutError, SessionConnectionLost) as e:
            exit_code = 1
            cleanup_reason = "run_failed"
            if not emitter:
                raise
            kind = "idle_timeout" if isinstance(e, TimeoutError) else "session_lost"
            exit_code = 4 if kind == "idle_timeout" else 3
            records.append(failure_record(
                kind, str(e) or kind, index=0, cell_id=None, block=source,
                session=live_session, duration_s=time.monotonic() - run_started,
                timeout=timeout,
            ))
            emitter.emit(records[-1])
        except Exception:
            # Genuine transport-level failure. Cleanup still happens via the
            # outer finally; surface non-zero exit so callers/CI notice.
            exit_code = 1
            cleanup_reason = "run_failed"
            raise
        else:
            exit_code = _exit_code_from_outputs(outputs)
            if exit_code != 0:
                cleanup_reason = "run_failed"
            run_duration = time.monotonic() - run_started
            event = {
                "code": payload, "outputs": outputs, "via": "run",
                **history_fields(outputs, block=source, duration_s=run_duration,
                                 exit_code=exit_code),
            }
            if emitter:
                records.append(build_cell_record(
                    outputs, index=0, cell_id=None, block=source,
                    duration_s=run_duration,
                    session=live_session, image_dir=image_dir,
                    exit_code=exit_code,
                ))
                add_seen_before(records[-1], state.history)
                emitter.emit(records[-1])
            state.history.log_event(name, "execution", event)
    finally:
        s.running = None
        state.store.update_if_present(s)
        # Best-effort runtime close (keeps remote kernel alive for --keep).
        try:
            runtime.stop()
        except Exception:
            pass

        if not keep:
            _teardown(name, s, reason=cleanup_reason)

    if emitter:
        duration = time.monotonic() - started
        emitter.emit(build_summary(
            records, duration_s=duration, session=name,
            exit_code=exit_code, output_dir=image_dir_used,
            cost=meter.block(duration) if meter else None,
        ))
    if exit_code != 0:
        raise typer.Exit(exit_code)


def _script_block(script: str, payload: str) -> Block:
    """Where the script's lines are inside the payload sent to the kernel."""
    body = read_script(script)
    stripped = _strip_shebang(body)
    return Block(
        label=script,
        file=script,
        code=stripped,
        prelude_lines=payload[: len(payload) - len(stripped)].count("\n"),
        first_line=1 + body[: len(body) - len(stripped)].count("\n"),
    )


def _teardown(name: str, s: SessionState, *, reason: str) -> None:
    """Best-effort full session teardown: ask the remote kernel to shut down,
    unassign the VM, and remove local state.

    Mirrors `commands.session.stop` but with a richer history event reason and
    swallowing all errors (we don't want a teardown failure to mask the user's
    exit code).
    """
    from colab_cli.common import state

    typer.echo(f"[colab] Stopping session '{name}'...", err=True)
    try:
        # The script may have outlived the proxy token.
        fresh = state.get_session(name, ignore_missing_session=True)
        if fresh:
            rt = ColabRuntime(
                fresh.url, fresh.token, kernel_id=fresh.kernel_id, session_name=fresh.name
            )
            rt.stop(shutdown_kernel=True)
    except Exception:
        pass

    try:
        state.client.unassign(s.endpoint)
    except Exception:
        pass

    try:
        state.store.remove(name)
    except Exception:
        pass

    try:
        state.history.log_event(name, "session_terminated", {"reason": reason})
    except Exception:
        pass
    typer.echo("[colab] Session terminated.", err=True)


def register(app: typer.Typer) -> None:
    # `context_settings` lets unknown args after the script path flow through
    # as positional `script_args` so users can pass `--flags-for-the-script`
    # without Typer trying to consume them.
    app.command(
        name="run",
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": True,
        },
    )(run_command)
