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

"""Keeping a runtime alive while a foreground command runs.

The Colab backend only counts kernel activity as use, so a runtime that is
serving an ssh port forward or a public tunnel idles out after ~90 minutes
even while traffic flows. The commands that need it ping the Tunnel Frontend
keep-alive endpoint from their own (foreground) process: no daemon and no
background thread, in line with the fire-and-forget design. Pinging stops
when the command exits.
"""

import subprocess
import time
from typing import Callable, Optional

import typer

from colab_cli.utils import get_status_code

KEEP_ALIVE_INTERVAL_SEC = 60


class _Pinger:
    """Pings once per call; warns once on errors, stops for a gone session."""

    def __init__(self, client, endpoint: str):
        self.client = client
        self.endpoint = endpoint
        self.active = True
        self._warned = False

    def ping(self) -> None:
        if not self.active:
            return
        try:
            self.client.keep_alive_assignment(self.endpoint)
        except Exception as e:  # A failed ping must never end the command.
            if get_status_code(e) == 404:
                self.active = False
                typer.echo(
                    "[colab] The runtime no longer exists; keep-alive stopped.",
                    err=True,
                )
            elif not self._warned:
                self._warned = True
                typer.echo(
                    f"[colab] keep-alive ping failed ({e}); retrying every "
                    f"{KEEP_ALIVE_INTERVAL_SEC}s.",
                    err=True,
                )


def wait_with_keep_alive(
    proc: subprocess.Popen,
    client,
    endpoint: str,
    interval: float = KEEP_ALIVE_INTERVAL_SEC,
) -> int:
    """Waits for ``proc``, pinging keep-alive every ``interval`` seconds.

    Returns:
      The process's exit code, or 130 after Ctrl+C (the child got the same
      SIGINT/CTRL_C_EVENT from the terminal and is waited for).
    """
    pinger = _Pinger(client, endpoint)
    pinger.ping()
    while True:
        try:
            return proc.wait(timeout=interval)
        except subprocess.TimeoutExpired:
            pinger.ping()
        except KeyboardInterrupt:
            proc.wait()
            return 130


def keep_alive_until_interrupted(
    client,
    endpoint: str,
    interval: float = KEEP_ALIVE_INTERVAL_SEC,
    duration: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Pings every ``interval`` seconds until Ctrl+C (or ``duration`` elapses)."""
    pinger = _Pinger(client, endpoint)
    start = clock()
    try:
        while pinger.active:
            pinger.ping()
            if duration is not None and clock() - start >= duration:
                return
            sleep(interval)
    except KeyboardInterrupt:
        return
