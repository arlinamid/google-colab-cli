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

"""`colab history`: what earlier runs teach (docs/08_agent_feedback.md, E).

Reads only the local history (~/.config/colab-cli/history); nothing is sent
anywhere. `colab log` shows one session's raw events; this aggregates all.
"""

import datetime
import sys
from typing import List, Optional

import typer
from typing_extensions import Annotated

from colab_cli.experience import error_groups, load_events, parse_since, stats
from colab_cli.results import SCHEMA, JsonEmitter

history_app = typer.Typer(
    help=(
        "Learn from earlier runs: repeated failures and what fixed them, and "
        "the usual duration and cost of each file."
    ),
    no_args_is_help=True,
)

SessionsOpt = Annotated[
    Optional[List[str]],
    typer.Option("-s", "--session", help="Only these sessions (repeatable). Default: all."),
]
JsonOpt = Annotated[bool, typer.Option("--json", envvar="COLAB_CLI_JSON", help="Print JSON records.")]


def _events(sessions, since: Optional[str]):
    from colab_cli.common import state

    start = None
    if since:
        try:
            start = datetime.datetime.now(datetime.timezone.utc) - parse_since(since)
        except ValueError as e:
            typer.echo(f"[colab] --since: {e}", err=True)
            raise typer.Exit(2)
    return load_events(state.history, sessions=sessions or None, since=start)


def _day(ts: Optional[str]) -> str:
    return (ts or "?")[:16].replace("T", " ")


def _steps_text(fix) -> str:
    if not fix:
        return ""
    parts = []
    for step in fix["steps"]:
        if "install" in step:
            parts.append("install " + " ".join(step["install"]))
        if "upload" in step:
            parts.append(f"upload {step['upload']}")
    if fix["code_changed"]:
        parts.append("a code change")
    return ", ".join(parts) or "running it again"


@history_app.command("errors")
def errors(
    session: SessionsOpt = None,
    since: Annotated[Optional[str], typer.Option("--since", help="Only this far back: 30m, 24h, 7d, 2w.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="At most this many groups.")] = 20,
    json_output: JsonOpt = False,
):
    """Past failures grouped by kind, newest first, with what fixed them."""
    groups = error_groups(_events(session, since), limit=limit)
    if json_output:
        emitter = JsonEmitter(sys.stdout)
        for g in groups:
            emitter.emit({"schema": SCHEMA, "type": "error_group", **g})
        return
    if not groups:
        typer.echo("[colab] No failures in the history.")
        return
    for g in groups:
        where = f" ({g['where']})" if g["where"] else ""
        typer.echo(f"{g['count']}x {g['code'] or 'unknown'}  {g['ename']}: {g['evalue']}{where}")
        line = f"   last seen {_day(g['last_seen'])} in {', '.join(g['sessions']) or '?'}"
        if g["last_fix"]:
            line += f"; fixed before by: {_steps_text(g['last_fix'])}"
        typer.echo(line)


@history_app.command("stats")
def file_stats(
    file: Annotated[Optional[str], typer.Argument(help="Only this file (by name).")] = None,
    session: SessionsOpt = None,
    since: Annotated[Optional[str], typer.Option("--since", help="Only this far back: 30m, 24h, 7d, 2w.")] = None,
    json_output: JsonOpt = False,
):
    """Runs, success rate, usual duration and cost per file, last run first."""
    rows = stats(_events(session, since), file=file)
    if json_output:
        emitter = JsonEmitter(sys.stdout)
        for r in rows:
            emitter.emit({"schema": SCHEMA, "type": "file_stats", **r})
        return
    if not rows:
        typer.echo("[colab] No runs in the history.")
        return
    for r in rows:
        text = f"{r['file']}: {r['runs']} runs, {round(r['success_rate'] * 100)}% ok"
        if r["median_duration_s"] is not None:
            text += f", median {r['median_duration_s']:.0f}s (max {r['max_duration_s']:.0f}s)"
        if r["median_cost_cu"] is not None:
            text += f", ~{r['median_cost_cu']} CU"
        text += f", last {_day(r['last_run'])}"
        if r["top_errors"]:
            text += "; errors: " + ", ".join(f"{k} x{v}" for k, v in r["top_errors"].items())
        typer.echo(text)


def register(app: typer.Typer):
    app.add_typer(history_app, name="history")
