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

import sys

import typer
from typing_extensions import Annotated

from colab_cli.consumption import format_consumption_status
from colab_cli.results import SCHEMA, JsonEmitter


def usage(
    json_output: Annotated[
        bool, typer.Option("--json", envvar="COLAB_CLI_JSON", help="Print one JSON record.")
    ] = False,
):
    """Show account compute-unit usage rate and balance"""
    from colab_cli.common import state

    try:
        result = state.client.get_consumption_user_info()
    except Exception as e:
        typer.echo(
            f"[colab] Warning: failed to fetch compute-unit info: {e}",
            err=True,
        )
        raise typer.Exit(code=1)

    if json_output:
        rate = result.consumption_rate_hourly
        JsonEmitter(sys.stdout).emit({
            "schema": SCHEMA,
            "type": "usage",
            "balance_cu": result.paid_compute_units_balance,
            "rate_cu_per_hour": rate,
            "hours_left_at_rate": (
                round(result.paid_compute_units_balance / rate, 1) if rate > 0 else None
            ),
            "assignments": result.assignments_count,
        })
        return
    typer.echo(format_consumption_status(result))


def register(app: typer.Typer):
    app.command()(usage)
