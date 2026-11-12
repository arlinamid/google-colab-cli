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

"""`colab inspect`: what the kernel and the machine look like right now.

For agents that need context without re-running code: variables (by type and
shape, never by value), GPU, disk, memory, packages installed since the first
inspect, and a file listing. A busy kernel is not queued behind: its sections
are reported as skipped (docs/08_agent_feedback.md, phase C).
"""

import sys
from typing import Optional

import requests
import typer
from typing_extensions import Annotated

from colab_cli.contents import ContentsClient
from colab_cli.inspect_vm import build_inspect_code, parse_inspect_output
from colab_cli.results import SCHEMA, JsonEmitter
from colab_cli.runtime import ColabRuntime
from colab_cli.utils import undo_msys_path

ALL_SECTIONS = ["vars", "gpu", "disk", "memory", "packages"]


def kernel_state(s) -> str:
    """The kernel's execution state from the Jupyter API, without using it.

    "idle" or "busy" (or "starting"), "none" when this session has no
    kernel yet, "unknown" when the runtime does not answer.
    """
    if not s.kernel_id:
        return "none"
    try:
        r = requests.get(
            f"{s.url.rstrip('/')}/api/kernels/{s.kernel_id}",
            params={"authuser": "0", "colab-runtime-proxy-token": s.token},
            timeout=10,
        )
    except requests.RequestException:
        return "unknown"
    if r.status_code == 404:
        return "none"
    if r.status_code != 200:
        return "unknown"
    return r.json().get("execution_state", "unknown")


def _print_human(rec) -> None:
    if rec.get("kernel"):
        typer.echo(f"kernel: {rec['kernel']}")
    gpu = rec.get("gpu")
    if isinstance(gpu, dict) and "devices" in gpu:
        if not gpu["devices"]:
            typer.echo("GPU: none")
        for d in gpu["devices"]:
            typer.echo(
                f"GPU: {d['name']}, {d['memory_used_mb']}/{d['memory_total_mb']} MB used, "
                f"{d['utilization_pct']}% busy"
            )
        if gpu.get("torch"):
            t = gpu["torch"]
            typer.echo(f"  torch: {t['allocated_mb']} MB allocated (peak {t['max_allocated_mb']} MB)")
    disk = rec.get("disk")
    if isinstance(disk, dict) and "free_gb" in disk:
        typer.echo(f"disk: {disk['free_gb']} GB free of {disk['total_gb']} GB")
    memory = rec.get("memory")
    if isinstance(memory, dict) and "total_gb" in memory:
        typer.echo(f"memory: {memory['available_gb']} GB available of {memory['total_gb']} GB")
    variables = rec.get("vars")
    if isinstance(variables, list):
        typer.echo(f"variables ({len(variables)}):")
        for v in variables:
            extra = " ".join(
                f"{k}={v[k]}" for k in ("len", "shape", "dtype", "device", "nbytes") if k in v
            )
            typer.echo(f"  {v['name']}: {v['type']} {extra}".rstrip())
    packages = rec.get("packages")
    if isinstance(packages, dict) and "count" in packages:
        if packages["baseline_created"]:
            typer.echo(f"packages: {packages['count']} installed (baseline saved for later diffs)")
        else:
            changes = [f"+{p['name']} {p['version']}" for p in packages["added"]]
            changes += [f"-{n}" for n in packages["removed"]]
            changes += [f"{p['name']} {p['from']}->{p['to']}" for p in packages["changed"]]
            typer.echo(
                f"packages: {packages['count']} installed; since the first inspect: "
                + (", ".join(changes) or "no changes")
            )
    for name in ALL_SECTIONS:
        value = rec.get(name)
        if isinstance(value, dict) and "skipped" in value:
            typer.echo(f"{name}: skipped ({value['skipped']})")
    files = rec.get("files")
    if files:
        typer.echo(f"files in {files['path']}:")
        for e in files["entries"]:
            suffix = "/" if e["type"] == "directory" else (f"  {e['size']} B" if e.get("size") is not None else "")
            typer.echo(f"  {e['name']}{suffix}")


def inspect(
    session: Annotated[Optional[str], typer.Option("-s", "--session", help="Session name")] = None,
    vars_: Annotated[bool, typer.Option("--vars", help="Variables in the kernel (type, length, shape, dtype, device; never values).")] = False,
    gpu: Annotated[bool, typer.Option("--gpu", help="GPU memory and use (nvidia-smi, torch).")] = False,
    disk: Annotated[bool, typer.Option("--disk", help="Free disk and memory.")] = False,
    packages: Annotated[bool, typer.Option("--packages", help="Packages installed or changed since the first inspect.")] = False,
    files: Annotated[Optional[str], typer.Option("--files", help="Also list this remote folder.")] = None,
    json_output: Annotated[bool, typer.Option("--json", envvar="COLAB_CLI_JSON", help="Print one JSON record.")] = False,
):
    """Show the kernel's variables and the machine's GPU, disk, memory and packages.

    With no section flag, shows all of them. Nothing is changed on the
    runtime, except that the first package check saves a baseline there.
    """
    from colab_cli.common import state

    sections = [n for n, on in (("vars", vars_), ("gpu", gpu), ("disk", disk), ("packages", packages)) if on]
    if disk:
        sections.insert(sections.index("disk") + 1, "memory")
    if not sections and not files:
        sections = list(ALL_SECTIONS)

    name = state.resolve_session(session)
    s = state.get_session(name)
    rec = {"schema": SCHEMA, "type": "inspect", "session": name, "kernel": None}

    if sections:
        rec["kernel"] = kernel_state(s)
        if rec["kernel"] == "busy":
            for section in sections:
                rec[section] = {"skipped": "kernel busy"}
        else:
            def on_started(kid):
                s.kernel_id = kid
                state.store.add(s)

            runtime = ColabRuntime(
                s.url, s.token, session_name=s.name, kernel_id=s.kernel_id,
                session_id=s.session_id, on_kernel_started=on_started,
            )
            try:
                outputs = runtime.execute_code(build_inspect_code(sections))
            finally:
                runtime.stop()
            data = parse_inspect_output(outputs)
            if data is None:
                errors = [o for o in outputs or [] if o.get("output_type") == "error"]
                detail = f"{errors[-1].get('ename')}: {errors[-1].get('evalue')}" if errors else "no reply"
                typer.echo(f"[colab] Inspect failed ({detail}).", err=True)
                raise typer.Exit(1)
            rec.update(data)

    if files:
        path = undo_msys_path(files)
        listing = ContentsClient(s).list_dir(path)
        entries = listing.get("content", []) if listing.get("type") == "directory" else [listing]
        rec["files"] = {
            "path": path,
            "entries": [
                {"name": e.get("name"), "type": e.get("type"), "size": e.get("size")}
                for e in sorted(entries, key=lambda e: (e.get("type") != "directory", e.get("name") or ""))
            ],
        }

    if json_output:
        JsonEmitter(sys.stdout).emit(rec)
    else:
        _print_human(rec)


def register(app: typer.Typer):
    app.command(name="inspect")(inspect)
