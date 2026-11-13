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

"""Sources that `colab job start` writes to the runtime (docs/08, phase B).

They are strings, not modules of this package: the standalone binary has no
.py files to read, and the runtime needs plain stdlib Python with no
colab_cli import. tests/test_job_vm.py runs them locally.

Files in the job folder (/content/.colab-jobs/<id>/):
  spec.json      id, name, argv, cwd (written by the CLI; argv only, no env)
  runner.py      RUNNER_SOURCE: runs argv, records everything below
  notebook.py    NOTEBOOK_SOURCE (notebook jobs): runs the cells of cells.json
  lib/colab_feedback.py  FEEDBACK_SOURCE, on the job's PYTHONPATH
  output.log     everything the job printed (stdout and stderr)
  tail.txt       its last 200 lines, so status reads stay small
  status.json    state, pid, times, exit_code, last_output_at, progress, cell
  metrics.jsonl  rows from colab_feedback.report()
  notes.jsonl    rows from colab_feedback.note()
  stop           created by `colab job stop`; the runner then ends the job
"""

RUNNER_SOURCE = r'''"""colab job runner: runs one job on the Colab VM, apart from the kernel."""
import codecs
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time

POLL = float(os.environ.get("COLAB_JOB_POLL", "1"))
STATUS_EVERY = float(os.environ.get("COLAB_JOB_STATUS_EVERY", "2"))
GRACE = float(os.environ.get("COLAB_JOB_GRACE", "10"))
TAIL_LINES = 200

_PCT = re.compile(r"(\d{1,3})%\|")
_COUNT = re.compile(r"\|\s*(\d+)/(\d+)\s")
_ETA = re.compile(r"<(?:(\d+):)?(\d+):(\d+)")
_LINE_BREAK = re.compile(r"(\r\n|\n|\r)")
_CELL = re.compile(r"^##colab-cell (\d+)/(\d+)(?: (.*))?$")


def parse_cell(line):
    """The cell a notebook job starts, from its marker line, or None."""
    m = _CELL.match(line)
    if not m:
        return None
    return {"index": int(m.group(1)), "total": int(m.group(2)), "label": (m.group(3) or "").strip()}


def parse_progress(segment):
    """tqdm-style progress in one output line, or None."""
    m = _PCT.search(segment)
    if not m:
        return None
    progress = {"percent": int(m.group(1)), "n": None, "total": None, "eta_s": None}
    count = _COUNT.search(segment, m.end())
    if count:
        progress["n"], progress["total"] = int(count.group(1)), int(count.group(2))
    eta = _ETA.search(segment, m.end())
    if eta:
        progress["eta_s"] = (
            int(eta.group(1) or 0) * 3600 + int(eta.group(2)) * 60 + int(eta.group(3))
        )
    return progress


def _replace(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Job:
    def __init__(self, folder):
        self.folder = folder
        with open(os.path.join(folder, "spec.json"), encoding="utf-8") as f:
            self.spec = json.load(f)
        self.status = {
            "id": self.spec["id"],
            "name": self.spec.get("name"),
            "argv": self.spec["argv"],
            "state": "starting",
            "pid": None,
            "started_at": time.time(),
            "ended_at": None,
            "exit_code": None,
            "last_output_at": None,
            "progress": None,
            "cell": None,
            "stop_reason": None,
        }
        self.tail = collections.deque(maxlen=TAIL_LINES)
        self.current = ""
        self.lock = threading.Lock()
        self.dirty = True

    def save(self):
        with self.lock:
            status = dict(self.status)
            lines = list(self.tail) + ([self.current] if self.current else [])
            self.dirty = False
        _replace(os.path.join(self.folder, "tail.txt"), "".join(line + "\n" for line in lines))
        _replace(os.path.join(self.folder, "status.json"), json.dumps(status))

    def feed(self, text):
        with self.lock:
            self.status["last_output_at"] = time.time()
            self.dirty = True
            for piece in _LINE_BREAK.split(text):
                if piece in ("\n", "\r\n"):
                    cell = parse_cell(self.current)
                    if cell:
                        self.status["cell"] = cell
                        self.status["progress"] = None  # a bar belongs to one cell
                    self.tail.append(self.current)
                    self.current = ""
                elif piece == "\r":
                    self.current = ""  # the line is drawn again (progress bars)
                elif piece:
                    self.current += piece
                    progress = parse_progress(self.current)
                    if progress:
                        self.status["progress"] = progress

    def run(self):
        log = open(os.path.join(self.folder, "output.log"), "ab")
        env = dict(os.environ)
        # Python children: UTF-8 and unbuffered, so the log and progress
        # follow the output as it is printed.
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        child = subprocess.Popen(
            self.spec["argv"],
            cwd=self.spec.get("cwd") or None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with self.lock:
            self.status.update(state="running", pid=child.pid)
        self.save()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        def read():
            while True:
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    break
                log.write(chunk)
                log.flush()
                self.feed(decoder.decode(chunk))
            rest = decoder.decode(b"", final=True)
            if rest:
                self.feed(rest)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        stop_file = os.path.join(self.folder, "stop")
        max_seconds = self.spec.get("max_seconds")
        stopping_since = None
        last_save = time.monotonic()
        while child.poll() is None:
            time.sleep(POLL)
            reason = None
            if stopping_since is None:
                if os.path.exists(stop_file):
                    reason = "request"
                elif max_seconds and time.time() - self.status["started_at"] >= max_seconds:
                    reason = "budget"  # --budget, converted to seconds at start
            if reason:
                stopping_since = time.monotonic()
                with self.lock:
                    self.status.update(state="stopping", stop_reason=reason)
                    self.dirty = True
                child.terminate()
            elif stopping_since is not None and time.monotonic() - stopping_since > GRACE:
                child.kill()
            if self.dirty and time.monotonic() - last_save >= STATUS_EVERY:
                self.save()
                last_save = time.monotonic()
        reader.join(timeout=10)
        log.close()
        code = child.returncode
        with self.lock:
            if self.current:
                self.tail.append(self.current)
                self.current = ""
            if stopping_since is not None:
                state = "stopped"
            else:
                state = "succeeded" if code == 0 else "failed"
            self.status.update(state=state, exit_code=code, ended_at=time.time())
        self.save()


def main(folder):
    job = Job(folder)
    try:
        job.run()
    except Exception as e:
        job.status.update(state="failed", ended_at=time.time(), error=f"{type(e).__name__}: {e}")
        job.save()
        raise


if __name__ == "__main__":
    main(sys.argv[1])
'''

NOTEBOOK_SOURCE = r'''"""colab job notebook driver: runs a notebook's code cells in order.

argv: cells.json [ARGS...]. cells.json is {"name": ..., "cells": [{"label",
"source"}]} (or the bare list). Each cell starts with a marker line,
"##colab-cell I/N LABEL", that the runner turns into status["cell"]. The cells
run in one IPython shell, so `!cmd`, `%magic` and `%%cell` magics work; the
first cell that raises ends the job with exit code 1. Without IPython (or with
COLAB_NOTEBOOK_PLAIN=1) the cells run as plain Python.
"""
import json
import os
import sys
import traceback


def _ipython_runner():
    try:
        from IPython.core.interactiveshell import InteractiveShell
    except ImportError:
        print("[colab] IPython is not available; running the cells as plain Python.", flush=True)
        return None
    shell = InteractiveShell.instance()
    for scheme in ("nocolor", "NoColor"):
        try:
            shell.run_line_magic("colors", scheme)
            break
        except Exception:
            continue
    # "Traceback (most recent call last):", the form `colab job` diagnoses.
    shell.InteractiveTB.set_mode("Plain")

    def run(source, label):
        result = shell.run_cell(source, store_history=True)
        sys.stdout.flush()
        return result.success

    return run


def _plain_runner():
    namespace = {"__name__": "__main__"}

    def run(source, label):
        try:
            exec(compile(source, "<" + label + ">", "exec"), namespace)
            return True
        except Exception:
            sys.stdout.flush()
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            return False

    return run


def main(cells_path, args):
    with open(cells_path, encoding="utf-8") as f:
        data = json.load(f)
    cells = data["cells"] if isinstance(data, dict) else data
    name = data.get("name") if isinstance(data, dict) else None
    sys.argv = [name or "notebook.ipynb", *args]
    run = None if os.environ.get("COLAB_NOTEBOOK_PLAIN") else _ipython_runner()
    run = run or _plain_runner()
    for i, cell in enumerate(cells, 1):
        label = cell.get("label") or ""
        print(("##colab-cell %d/%d %s" % (i, len(cells), label)).rstrip(), flush=True)
        if not run(cell["source"], label or "cell %d" % i):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
'''

FEEDBACK_SOURCE = r'''"""Report progress from a `colab job`: from colab_feedback import report, note.

Outside a job (no COLAB_JOB_DIR) both functions do nothing, so the same
script still runs anywhere.
"""
import json
import os
import time

_DIR = os.environ.get("COLAB_JOB_DIR")


def _append(name, row):
    if not _DIR:
        return
    with open(os.path.join(_DIR, name), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def report(step=None, **metrics):
    """Record metrics, e.g. report(step=120, loss=0.41, val_acc=0.88).

    epoch= and epochs= (epoch counted from 1), or step= and total=, also tell
    `colab job status` how far the whole run is ("overall"), e.g.
    report(epoch=2, epochs=5, loss=0.31).
    """
    row = {"t": time.time()}
    if step is not None:
        row["step"] = step
    row.update(metrics)
    _append("metrics.jsonl", row)


def note(text):
    """Record an event for whoever checks the job, e.g. note("lr=1e-4")."""
    _append("notes.jsonl", {"t": time.time(), "text": str(text)})
'''
