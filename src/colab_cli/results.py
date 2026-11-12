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

"""Machine-readable results for `--json`: one record per cell, then a summary.

Field names follow the upstream `colab exec --json` proposal
(googlecolab/google-colab-cli#237: cell_index, cell_id, status, stdout,
stderr, data, images, error) and add error.location, diagnosis, duration_s
and session. Every record carries ``"schema": "colab.v1"``; v1 only ever
gains fields. See docs/08_agent_feedback.md.
"""

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from colab_cli.diagnosis import diagnose, diagnose_failure

SCHEMA = "colab.v1"
# stdout, stderr and tracebacks keep their last MAX_TEXT characters; the
# history log keeps everything.
MAX_TEXT = 64 * 1024

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_TEXT_MIMES = ("text/plain", "application/json", "text/markdown")
_IMAGE_MIMES = (("image/png", "png"), ("image/jpeg", "jpg"))

# A file that holds code sent to the kernel (as opposed to a library file).
_CELL_FILE_RE = re.compile(
    r"ipykernel_\d+[/\\]\d+\.py|<ipython-input-\d+-[0-9a-f]+>|^Cell In\[\d+\]"
)
# IPython 7 (Colab): "/tmp/ipykernel_1/2.py in <cell line: 0>()", followed by
# context lines where "---->" marks the line that ran.
_V7_FRAME_RE = re.compile(
    r"^(?P<file>\S+\.py|<ipython-input-[^>]+>) in "
    r"(?P<func><cell line: \d+>|<module>|[^\s(]+)"
)
_MARK_RE = re.compile(r"^-+>\s*(\d+)")
# IPython 8: "Cell In[3], line 4" and "File /path/x.py:12, in f()".
_V8_CELL_RE = re.compile(r"^Cell In\[(\d+)\], line (\d+)")
_V8_FILE_RE = re.compile(r"^File (\S+?):(\d+)(?:, in (.+))?$")
# SyntaxError: '  File "/tmp/ipykernel_1/2.py", line 2'.
_SYNTAX_RE = re.compile(r'^\s*File "([^"]+)", line (\d+)')


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass
class Block:
    """Where a piece of executed code came from, to map tracebacks back.

    ``prelude_lines`` lines were sent before the user's code (e.g. --env);
    ``first_line`` is the file line of the code's first line (2 when
    `colab run` stripped a shebang).
    """

    label: str
    file: Optional[str] = None
    cell: Optional[int] = None
    code: str = ""
    prelude_lines: int = 0
    first_line: int = 1


def _frames(lines: List[str]) -> List[Dict[str, Any]]:
    frames: List[Dict[str, Any]] = []
    current = None
    for line in lines:
        if m := _V8_CELL_RE.match(line):
            frames.append({"file": f"Cell In[{m.group(1)}]", "func": "<module>", "line": int(m.group(2))})
            current = None
        elif m := _SYNTAX_RE.match(line):
            frames.append({"file": m.group(1), "func": None, "line": int(m.group(2))})
            current = None
        elif m := _V8_FILE_RE.match(line):
            frames.append({"file": m.group(1), "func": m.group(3), "line": int(m.group(2))})
            current = None
        elif m := _V7_FRAME_RE.match(line):
            current = {"file": m.group("file"), "func": m.group("func"), "line": None}
            frames.append(current)
        elif current is not None and current["line"] is None and (m := _MARK_RE.match(line)):
            current["line"] = int(m.group(1))
    return frames


def map_location(traceback: List[str], block: Block) -> Optional[Dict[str, Any]]:
    """Map a kernel traceback to the line of the user's code that failed.

    Returns ``{"file", "cell", "line", "code"}`` or None when the traceback
    has no frame in the executed code. ``line`` is None when the error was
    in the prelude the CLI added.
    """
    lines = strip_ansi("\n".join(traceback or [])).splitlines()
    frames = _frames(lines)
    cell_frames = [f for f in frames if _CELL_FILE_RE.search(f["file"])]
    if not cell_frames:
        return None
    # The executed cell is the top-level frame of the last exception in a
    # chain; a function from an earlier cell lives in another file.
    top = [f for f in cell_frames if f["func"] in (None, "<module>") or str(f["func"]).startswith("<cell line")]
    ours = (top[-1] if top else cell_frames[0])["file"]
    hits = [f for f in frames if f["file"] == ours and f["line"] is not None]
    if not hits:
        return None
    cell_line = hits[-1]["line"]
    code_line = cell_line - block.prelude_lines
    code_lines = block.code.splitlines()
    return {
        "file": block.file,
        "cell": block.cell,
        "line": code_line + block.first_line - 1 if code_line >= 1 else None,
        "code": code_lines[code_line - 1].strip() if 1 <= code_line <= len(code_lines) else None,
    }


def _describe_errors(errors, block, session, exit_code):
    """The error and diagnosis of a block, from its error outputs."""
    if exit_code is not None:
        exits = [e for e in errors if e.get("ename") == "SystemExit"]
        others = [e for e in errors if e.get("ename") != "SystemExit"]
        errors = others or (exits if exit_code != 0 else [])
    if not errors:
        return None, None
    e = errors[-1]
    tb = strip_ansi("\n".join(e.get("traceback") or []))
    error = {
        "ename": e.get("ename", "Error"),
        "evalue": e.get("evalue", ""),
        "traceback": _tail(tb)[0],
        "location": map_location(e.get("traceback") or [], block),
    }
    return error, diagnose(error["ename"], error["evalue"], tb, session=session, file=block.file)


def history_fields(
    outputs: List[Dict[str, Any]],
    *,
    block: Block,
    duration_s: float,
    exit_code: Optional[int] = None,
    cost_cu: Optional[float] = None,
) -> Dict[str, Any]:
    """What an `execution` history event stores besides code and outputs.

    Used by `colab history` and `seen_before` (phase E), with or without
    --json.
    """
    errors = [o for o in outputs or [] if o.get("output_type") == "error"]
    error, diagnosis = _describe_errors(errors, block, None, exit_code)
    fields = {
        "status": "error" if error else "ok",
        "file": block.file,
        "label": block.label,
        "cell": block.cell,
        "duration_s": round(duration_s, 2),
        "error": (
            {k: error[k] for k in ("ename", "evalue", "location")} if error else None
        ),
        "diagnosis": diagnosis["code"] if diagnosis else None,
    }
    if cost_cu is not None:
        fields["cost_cu"] = cost_cu
    return fields


def _tail(text: str):
    if len(text) > MAX_TEXT:
        return text[-MAX_TEXT:], True
    return text, False


def _save_image(b64: str, directory: str, name: str) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.abspath(os.path.join(directory, name))
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return path


def build_cell_record(
    outputs: List[Dict[str, Any]],
    *,
    index: int,
    cell_id: Optional[str],
    block: Block,
    duration_s: float,
    session: Optional[str],
    image_dir: Callable[[], str],
    exit_code: Optional[int] = None,
) -> Dict[str, Any]:
    """One result record for one executed block.

    ``image_dir`` is called only when there is an image to save. Pass
    ``exit_code`` for `colab run`, where SystemExit is the script's exit
    status: 0 is success, anything else an error with code script_exit.
    """
    streams = {"stdout": [], "stderr": []}
    data: List[Dict[str, str]] = []
    images: List[str] = []
    errors = []
    for out in outputs or []:
        kind = out.get("output_type")
        if kind == "stream" or (kind is None and "text" in out and "data" not in out):
            name = "stderr" if out.get("name") == "stderr" else "stdout"
            streams[name].append(out.get("text", ""))
        elif "data" in out:
            payload = out["data"]
            saved = False
            for mime, ext in _IMAGE_MIMES:
                if mime in payload:
                    images.append(
                        _save_image(payload[mime], image_dir(), f"cell{index}_{len(images) + 1}.{ext}")
                    )
                    saved = True
            if saved:
                continue
            for mime in _TEXT_MIMES:
                if mime in payload:
                    value = payload[mime]
                    if not isinstance(value, str):
                        value = json.dumps(value, ensure_ascii=False)
                    data.append({"mime": mime, "text": value})
        elif kind == "error":
            errors.append(out)

    error, diagnosis = _describe_errors(errors, block, session, exit_code)

    stdout, stdout_cut = _tail("".join(streams["stdout"]))
    stderr, stderr_cut = _tail("".join(streams["stderr"]))
    record = {
        "schema": SCHEMA,
        "type": "cell",
        "cell_index": index,
        "cell_id": cell_id,
        "status": "error" if error else "ok",
        "stdout": stdout,
        "stdout_truncated": stdout_cut,
        "stderr": stderr,
        "stderr_truncated": stderr_cut,
        "data": data,
        "images": images,
        "error": error,
        "diagnosis": diagnosis,
        "duration_s": round(duration_s, 2),
        "session": session,
    }
    if exit_code is not None:
        record["exit_code"] = exit_code
    return record


_FAILURE_ENAMES = {
    "idle_timeout": "TimeoutError",
    "session_lost": "SessionConnectionLost",
    "budget_reached": "RunLimitExceeded",
}


def failure_record(
    kind: str,
    message: str,
    *,
    index: int,
    cell_id: Optional[str],
    block: Block,
    session: Optional[str],
    duration_s: float,
    timeout: Optional[float] = None,
    budget: Optional[float] = None,
) -> Dict[str, Any]:
    """A record for a block the CLI could not finish (timeout, budget, lost session)."""
    return {
        "schema": SCHEMA,
        "type": "cell",
        "cell_index": index,
        "cell_id": cell_id,
        "status": "error",
        "stdout": "",
        "stdout_truncated": False,
        "stderr": "",
        "stderr_truncated": False,
        "data": [],
        "images": [],
        "error": {
            "ename": _FAILURE_ENAMES.get(kind, "Error"),
            "evalue": message,
            "traceback": "",
            "location": None,
        },
        "diagnosis": diagnose_failure(kind, session=session, timeout=timeout, budget=budget),
        "duration_s": round(duration_s, 2),
        "session": session,
    }


def build_summary(
    records: List[Dict[str, Any]],
    *,
    duration_s: float,
    session: Optional[str],
    exit_code: int,
    output_dir: Optional[str],
    cost: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    failed = sum(1 for r in records if r.get("status") != "ok")
    return {
        "schema": SCHEMA,
        "type": "summary",
        "status": "ok" if exit_code == 0 and not failed else "error",
        "cells_run": len(records),
        "cells_failed": failed,
        "exit_code": exit_code,
        "duration_s": round(duration_s, 2),
        "session": session,
        "output_dir": output_dir,
        "cost": cost,
    }


class JsonEmitter:
    """Writes one JSON object per line to the real stdout."""

    def __init__(self, stream):
        self.stream = stream

    def emit(self, record: Dict[str, Any]) -> None:
        self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.stream.flush()
