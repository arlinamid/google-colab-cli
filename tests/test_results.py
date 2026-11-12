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

"""Machine-readable result records (`--json`) and traceback line mapping.

The traceback samples are copied from real Colab kernels (IPython 7 style
with ANSI colours); the IPython 8 forms are covered too.
"""

import base64
import os

from colab_cli.results import (
    MAX_TEXT,
    SCHEMA,
    Block,
    build_cell_record,
    build_summary,
    failure_record,
    map_location,
    strip_ansi,
)

E = "\x1b"

# `1/0` in a stdin cell.
TB_SIMPLE = [
    f"{E}[0;31m---------------------------------------------------------------------------{E}[0m",
    f"{E}[0;31mZeroDivisionError{E}[0m                         Traceback (most recent call last)",
    f"{E}[0;32m/tmp/ipykernel_905/342807933.py{E}[0m in {E}[0;36m<cell line: 0>{E}[0;34m(){E}[0m\n"
    f"{E}[0;32m----> 1{E}[0;31m {E}[0m1{E}[0;34m/{E}[0m{E}[0;36m0{E}[0m\n{E}[0m",
    f"{E}[0;31mZeroDivisionError{E}[0m: division by zero",
]

# Error on cell line 5, then two library frames.
TB_LIBRARY = [
    "---------------------------------------------------------------------------",
    "LibsndfileError                           Traceback (most recent call last)",
    "/tmp/ipykernel_444/1373453813.py in <cell line: 0>()\n"
    "      3 t = time.time()\n"
    "      4 m = load()\n"
    '----> 5 audio, _ = sf.read("/content/a.wav")\n'
    "      6 print(audio)\n",
    "/usr/local/lib/python3.13/dist-packages/soundfile.py in read(file, frames)\n"
    "    312 \n"
    "--> 313     with SoundFile(file, 'r') as f:\n",
    "LibsndfileError: Error opening '/content/a.wav'",
]

# A function defined in the same cell fails: the innermost cell frame wins.
TB_FUNCTION = [
    "---------------------------------------------------------------------------",
    "ValueError                                Traceback (most recent call last)",
    "/tmp/ipykernel_7/11.py in <cell line: 0>()\n"
    "      4 \n"
    "----> 5 f()\n",
    "/tmp/ipykernel_7/11.py in f()\n"
    "      1 def f():\n"
    "----> 2     raise ValueError('x')\n",
    "ValueError: x",
]

TB_SYNTAX = [
    f'{E}[0;36m  File {E}[0;32m"/tmp/ipykernel_3783/4226116853.py"{E}[0;36m, line {E}[0;32m2{E}[0m\n'
    f'{E}[0;31m    open("/content/x.txt","w").write("original{E}[0m\n'
    f"{E}[0m                                           ^{E}[0m\n"
    f"{E}[0;31mSyntaxError{E}[0m{E}[0;31m:{E}[0m unterminated string literal (detected at line 2)\n",
]

TB_IPYTHON8 = [
    "---------------------------------------------------------------------------",
    "ZeroDivisionError                         Traceback (most recent call last)",
    "Cell In[3], line 4\n      2 x = 1\n----> 4 x / 0\n",
    "ZeroDivisionError: division by zero",
]


def test_strip_ansi():
    assert strip_ansi(f"{E}[0;31mRed{E}[0m text") == "Red text"


def test_map_simple_stdin_cell():
    block = Block(label="stdin", file="<stdin>", code="1/0\n")
    assert map_location(TB_SIMPLE, block) == {
        "file": "<stdin>",
        "cell": None,
        "line": 1,
        "code": "1/0",
    }


def test_map_skips_library_frames_and_subtracts_the_prelude():
    code = "\n".join(f"line{i}" for i in range(1, 7)) + "\n"
    # Two prelude lines were sent before the user's code (e.g. --env).
    block = Block(label="a.py", file="a.py", code=code, prelude_lines=2)
    loc = map_location(TB_LIBRARY, block)
    assert loc["file"] == "a.py"
    assert loc["line"] == 3
    assert loc["code"] == "line3"


def test_map_uses_the_innermost_frame_of_the_cell():
    block = Block(label="a.py", file="a.py", code="def f():\n    raise ValueError('x')\n")
    loc = map_location(TB_FUNCTION, block)
    assert loc["line"] == 2
    assert loc["code"] == "raise ValueError('x')"


def test_map_shebang_offset_via_first_line():
    """`colab run` strips a shebang: cell line 1 is file line 2."""
    block = Block(label="s.py", file="s.py", code="1/0\n", first_line=2)
    assert map_location(TB_SIMPLE, block)["line"] == 2


def test_map_notebook_cell():
    block = Block(label="cell 4", file="nb.ipynb", cell=4, code="1/0")
    loc = map_location(TB_SIMPLE, block)
    assert (loc["file"], loc["cell"], loc["line"]) == ("nb.ipynb", 4, 1)


def test_map_syntax_error():
    block = Block(label="a.py", file="a.py", code='x = 1\nopen("/content/x.txt","w").write("original\n')
    loc = map_location(TB_SYNTAX, block)
    assert loc["line"] == 2


def test_map_ipython8_cell_frame():
    block = Block(label="a.py", file="a.py", code="a\nx = 1\nb\nx / 0\n")
    assert map_location(TB_IPYTHON8, block)["line"] == 4


def test_map_error_inside_the_prelude_has_no_line():
    block = Block(label="a.py", file="a.py", code="print(1)\n", prelude_lines=3)
    loc = map_location(TB_SIMPLE, block)
    assert loc["line"] is None


def test_map_without_a_cell_frame():
    assert map_location(["ValueError: x"], Block(label="x", file="a.py")) is None


def _block():
    return Block(label="a.py", file="a.py", code="1/0\n")


def test_ok_record_shape(tmp_path):
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": "hello\n"},
        {"output_type": "stream", "name": "stderr", "text": "warn\n"},
        {"output_type": "execute_result", "data": {"text/plain": "42"}},
    ]
    rec = build_cell_record(
        outputs, index=0, cell_id=None, block=_block(), duration_s=1.234,
        session="s1", image_dir=lambda: str(tmp_path),
    )
    assert rec["schema"] == SCHEMA
    assert rec["type"] == "cell"
    assert rec["status"] == "ok"
    assert rec["stdout"] == "hello\n"
    assert rec["stderr"] == "warn\n"
    assert rec["data"] == [{"mime": "text/plain", "text": "42"}]
    assert rec["images"] == []
    assert rec["error"] is None
    assert rec["diagnosis"] is None
    assert rec["duration_s"] == 1.23
    assert rec["session"] == "s1"
    assert rec["stdout_truncated"] is False


def test_error_record_has_plain_traceback_location_and_diagnosis(tmp_path):
    outputs = [
        {
            "output_type": "error",
            "ename": "ZeroDivisionError",
            "evalue": "division by zero",
            "traceback": TB_SIMPLE,
        }
    ]
    rec = build_cell_record(
        outputs, index=2, cell_id="abc", block=_block(), duration_s=0.5,
        session="s1", image_dir=lambda: str(tmp_path),
    )
    assert rec["status"] == "error"
    assert rec["cell_index"] == 2
    assert rec["cell_id"] == "abc"
    err = rec["error"]
    assert err["ename"] == "ZeroDivisionError"
    assert E not in err["traceback"]
    assert err["location"]["line"] == 1
    assert rec["diagnosis"]["code"] == "unknown"


def test_images_are_saved_and_reported_by_path(tmp_path):
    png = base64.b64encode(b"\x89PNG fake").decode()
    outputs = [{"output_type": "display_data", "data": {"image/png": png, "text/plain": "<Figure>"}}]
    rec = build_cell_record(
        outputs, index=1, cell_id=None, block=_block(), duration_s=0,
        session="s", image_dir=lambda: str(tmp_path),
    )
    assert len(rec["images"]) == 1
    path = rec["images"][0]
    assert os.path.isabs(path)
    assert open(path, "rb").read() == b"\x89PNG fake"
    # The figure's text repr is not useful next to the image.
    assert rec["data"] == []


def test_long_streams_keep_the_tail(tmp_path):
    text = "x" * (MAX_TEXT + 10) + "END"
    outputs = [{"output_type": "stream", "name": "stdout", "text": text}]
    rec = build_cell_record(
        outputs, index=0, cell_id=None, block=_block(), duration_s=0,
        session="s", image_dir=lambda: str(tmp_path),
    )
    assert rec["stdout_truncated"] is True
    assert rec["stdout"].endswith("END")
    assert len(rec["stdout"]) == MAX_TEXT


def test_run_exit_zero_is_ok_and_nonzero_is_an_error(tmp_path):
    def rec_for(code):
        outputs = [{"output_type": "error", "ename": "SystemExit", "evalue": code, "traceback": []}]
        return build_cell_record(
            outputs, index=0, cell_id=None, block=_block(), duration_s=0,
            session=None, image_dir=lambda: str(tmp_path), exit_code=int(code),
        )

    ok = rec_for("0")
    assert ok["status"] == "ok" and ok["error"] is None and ok["exit_code"] == 0
    bad = rec_for("3")
    assert bad["status"] == "error"
    assert bad["exit_code"] == 3
    assert bad["diagnosis"]["code"] == "script_exit"


def test_failure_record():
    rec = failure_record(
        "idle_timeout", "No output for 30s.", index=0, cell_id=None,
        block=_block(), session="s", duration_s=30.0, timeout=30,
    )
    assert rec["status"] == "error"
    assert rec["error"]["ename"] == "TimeoutError"
    assert rec["diagnosis"]["code"] == "idle_timeout"


def test_summary():
    recs = [{"status": "ok", "duration_s": 1}, {"status": "error", "duration_s": 2}]
    s = build_summary(recs, duration_s=3.21, session="s", exit_code=1, output_dir=None)
    assert s == {
        "schema": SCHEMA,
        "type": "summary",
        "status": "error",
        "cells_run": 2,
        "cells_failed": 1,
        "exit_code": 1,
        "duration_s": 3.21,
        "session": "s",
        "output_dir": None,
        "cost": None,
    }
