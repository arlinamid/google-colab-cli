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

"""CLI-side job logic: ids, the start code, and the job record (phase B)."""

import json
import re

import pytest

from colab_cli.jobs import (
    JOBS_DIR,
    build_start_code,
    diagnose_log,
    job_argv,
    job_record,
    metric_trend,
    new_job_id,
    new_lines,
)


def test_job_id_is_sortable_and_safe():
    jid = new_job_id("My Train/run!")
    assert re.fullmatch(r"my-train-run-\d{8}-\d{6}-[0-9a-f]{4}", jid)
    assert re.fullmatch(r"job-\d{8}-\d{6}-[0-9a-f]{4}", new_job_id(None))


def test_job_argv():
    assert job_argv("train.py", None, ["--lr", "1e-4"]) == ["{python}", "-u", "{dir}/script.py", "--lr", "1e-4"]
    assert job_argv(None, ["nvidia-smi -l 5"], []) == ["bash", "-c", "nvidia-smi -l 5"]
    assert job_argv(None, ["python", "x.py", "a b"], []) == ["python", "x.py", "a b"]


def test_start_code_compiles_and_hides_env_from_the_spec():
    code = build_start_code(
        "j-1", name="j", argv=["{python}", "-u", "{dir}/script.py"],
        env={"HF_TOKEN": "secret-value"}, script_source="print('hi')\n",
    )
    compile(code, "<start>", "exec")
    assert f"{JOBS_DIR}/j-1" in code
    assert "start_new_session=True" in code
    # The spec file holds argv only; env goes straight to the process.
    spec_literal = re.search(r"_spec = (.*)\n", code).group(1)
    assert "secret-value" not in spec_literal


def test_start_code_runs_locally(tmp_path, monkeypatch):
    """Execute the start code with a fake jobs dir and a fake Popen."""
    import subprocess

    started = {}

    class FakePopen:
        pid = 4242

        def __init__(self, argv, **kw):
            started["argv"], started["kw"] = argv, kw

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    code = build_start_code(
        "j-2", name="j", argv=["{python}", "-u", "{dir}/script.py", "x"],
        env={"A": "1"}, script_source="print('hi')\n",
        jobs_dir=str(tmp_path).replace("\\", "/"),
    )
    out = {}
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<start>", "exec"), out)
    line = [x for x in buf.getvalue().splitlines() if x.startswith("COLAB_JOB ")][0]
    info = json.loads(line[len("COLAB_JOB "):])
    assert info["id"] == "j-2" and info["pid"] == 4242
    job_dir = tmp_path / "j-2"
    spec = json.loads((job_dir / "spec.json").read_text())
    assert spec["argv"][2].endswith("/j-2/script.py")
    assert spec["argv"][3] == "x"
    assert (job_dir / "script.py").read_text() == "print('hi')\n"
    assert (job_dir / "runner.py").exists()
    assert (job_dir / "lib" / "colab_feedback.py").exists()
    env = started["kw"]["env"]
    assert env["A"] == "1" and env["COLAB_JOB_DIR"].endswith("j-2")
    assert started["kw"]["start_new_session"] is True


def test_metric_trend():
    rows = [{"t": 1, "step": 1, "loss": 1.0}, {"t": 2, "step": 2, "loss": 0.5, "acc": 0.7}, {"t": 3, "step": 3, "loss": 0.7}]
    assert metric_trend(rows) == {
        "loss": {"first": 1.0, "last": 0.7, "min": 0.5, "max": 1.0},
        "acc": {"first": 0.7, "last": 0.7, "min": 0.7, "max": 0.7},
    }


def test_diagnose_log_reads_the_last_traceback():
    tail = (
        "epoch 1\nTraceback (most recent call last):\n"
        '  File "/content/.colab-jobs/j/script.py", line 3, in <module>\n'
        "    import torchvision\n"
        "ModuleNotFoundError: No module named 'torchvision'\n"
    )
    d = diagnose_log(tail, session="s", file="train.py")
    assert d["code"] == "missing_module"
    assert d["next_actions"][0]["cmd"] == "colab install -s s torchvision"


def test_diagnose_log_dotted_exception_name():
    tail = "Traceback (most recent call last):\n  x\ntorch.OutOfMemoryError: CUDA out of memory.\n"
    assert diagnose_log(tail, session="s")["code"] == "cuda_oom"


def test_diagnose_log_without_a_traceback():
    assert diagnose_log("killed\n", session="s")["code"] == "unknown"


STATUS = {
    "id": "j-1", "name": "train", "argv": ["python", "-u", "x.py"], "state": "running",
    "pid": 10, "started_at": 1000.0, "ended_at": None, "exit_code": None,
    "last_output_at": 1500.0, "progress": {"percent": 40, "n": 4, "total": 10, "eta_s": 90},
}


def test_running_record():
    rec = job_record(STATUS, metrics=[{"t": 1, "step": 4, "loss": 0.3}], tail="a\nb\n",
                     now=1600.0, stall_after=600, session="s", file="train.py")
    assert rec["schema"] == "colab.v1" and rec["type"] == "job"
    assert rec["id"] == "j-1" and rec["state"] == "running"
    assert rec["elapsed_s"] == 600.0
    assert rec["silent_s"] == 100.0
    assert rec["stalled"] is False
    assert rec["progress"]["percent"] == 40
    assert rec["metrics"]["last"] == {"step": 4, "loss": 0.3}
    assert rec["metrics"]["count"] == 1
    assert rec["tail"] == ["a", "b"]
    assert rec["diagnosis"] is None
    assert rec["session"] == "s"


def test_stalled_when_silent_too_long():
    rec = job_record(STATUS, metrics=[], tail="", now=2200.0, stall_after=600, session="s")
    assert rec["stalled"] is True
    assert rec["diagnosis"]["code"] == "stalled"
    assert any("colab job tail" in (a["cmd"] or "") for a in rec["diagnosis"]["next_actions"])


def test_failed_record_has_a_diagnosis():
    st = dict(STATUS, state="failed", exit_code=1, ended_at=1700.0)
    tail = "Traceback (most recent call last):\n  x\nOSError: [Errno 28] No space left on device\n"
    rec = job_record(st, metrics=[], tail=tail, now=9999.0, stall_after=600, session="s")
    assert rec["elapsed_s"] == 700.0
    assert rec["stalled"] is False
    assert rec["diagnosis"]["code"] == "disk_full"


def test_record_tail_is_limited():
    tail = "".join(f"line{i}\n" for i in range(100))
    rec = job_record(STATUS, metrics=[], tail=tail, now=1600.0, stall_after=600,
                     session="s", tail_lines=5)
    assert rec["tail"] == [f"line{i}" for i in range(95, 100)]


@pytest.mark.parametrize(
    "prev, cur, new",
    [
        ([], ["a", "b"], ["a", "b"]),
        (["a", "b"], ["a", "b", "c"], ["c"]),
        (["a", "b", "c"], ["b", "c", "d", "e"], ["d", "e"]),
        (["x"], ["y", "z"], ["y", "z"]),
        (["a", "b"], ["a", "b"], []),
    ],
)
def test_new_lines(prev, cur, new):
    assert new_lines(prev, cur) == new


def test_budget_stop_has_a_diagnosis():
    st = dict(STATUS, state="stopped", exit_code=-15, ended_at=1700.0, stop_reason="budget")
    rec = job_record(st, metrics=[], tail="", now=9999.0, stall_after=600, session="s")
    assert rec["stop_reason"] == "budget"
    assert rec["diagnosis"]["code"] == "budget_reached"


def test_start_code_carries_max_seconds():
    code = build_start_code("j-3", name="j", argv=["sleep", "9"], env={}, max_seconds=1800.0)
    assert "'max_seconds': 1800.0" in code


# --- progress across the whole run, notebook jobs (docs/09) ---

def _rec(status=None, metrics=(), now=1600.0):
    st = dict(STATUS, **(status or {}))
    return job_record(st, metrics=list(metrics), tail="", now=now, stall_after=600, session="s")


def test_overall_percent_from_epochs_and_the_bar_inside_the_epoch():
    rec = _rec(metrics=[{"t": 1, "epoch": 2, "epochs": 5, "loss": 0.3}])
    # epoch 2 of 5 running, its bar at 40%: (1 + 0.4) / 5
    assert rec["overall_percent"] == 28.0
    assert rec["epoch"] == {"current": 2, "total": 5}
    assert set(rec["metrics"]["trend"]) == {"loss"}


def test_overall_percent_from_step_and_total():
    rec = _rec({"progress": None}, metrics=[{"t": 1, "step": 30, "total": 120}])
    assert rec["overall_percent"] == 25.0
    assert rec["epoch"] is None


def test_overall_percent_from_notebook_cells():
    rec = _rec({"cell": {"index": 3, "total": 4, "label": "cell 3"},
                "progress": {"percent": 50, "n": None, "total": None, "eta_s": None}})
    assert rec["overall_percent"] == 62.5
    assert rec["cell"] == {"index": 3, "total": 4, "label": "cell 3"}


def test_overall_percent_falls_back_to_the_bar_or_none():
    assert _rec()["overall_percent"] == 40.0
    assert _rec({"progress": None})["overall_percent"] is None
    assert _rec({"progress": None})["cell"] is None


def test_overall_percent_is_100_when_succeeded_and_clamped():
    assert _rec({"state": "succeeded", "ended_at": 1600.0, "exit_code": 0})["overall_percent"] == 100.0
    rec = _rec(metrics=[{"t": 1, "epoch": 9, "epochs": 5}])
    assert rec["overall_percent"] == 100.0


def test_last_output_age_also_after_the_job_ended():
    assert _rec()["last_output_age_s"] == 100.0
    done = _rec({"state": "failed", "ended_at": 1550.0, "exit_code": 1}, now=1700.0)
    assert done["silent_s"] is None and done["last_output_age_s"] == 200.0
    assert _rec({"last_output_at": None, "started_at": None})["last_output_age_s"] is None


def test_job_argv_for_a_notebook():
    assert job_argv("nb.ipynb", None, ["--x"]) == [
        "{python}", "-u", "{dir}/notebook.py", "{dir}/cells.json", "--x",
    ]


def _write_nb(path, cells):
    import nbformat

    nb = nbformat.v4.new_notebook()
    nb.cells = cells
    nbformat.write(nb, str(path))


def test_notebook_cells_and_job_files(tmp_path):
    import nbformat

    from colab_cli.job_vm import NOTEBOOK_SOURCE
    from colab_cli.jobs import job_files, notebook_cells

    path = tmp_path / "train.ipynb"
    _write_nb(path, [
        nbformat.v4.new_markdown_cell("# Title"),
        nbformat.v4.new_code_cell("#@title Setup\n!pip install -q x\nimport x"),
        nbformat.v4.new_code_cell("   \n"),
        nbformat.v4.new_code_cell("x.train()"),
    ])

    cells = notebook_cells(str(path))
    assert [c["label"] for c in cells] == ["cell 1 (Setup)", "cell 3"]
    assert cells[0]["source"].startswith("#@title Setup\n!pip")

    files = job_files(str(path))
    assert files["notebook.py"] == NOTEBOOK_SOURCE
    data = json.loads(files["cells.json"])
    assert data["name"] == "train.ipynb" and data["cells"] == cells

    script = tmp_path / "s.py"
    script.write_text("print(1)\n", encoding="utf-8")
    assert job_files(str(script)) == {"script.py": "print(1)\n"}


def test_start_code_writes_extra_files(tmp_path, monkeypatch):
    import contextlib
    import io
    import subprocess

    class FakePopen:
        pid = 1

        def __init__(self, argv, **kw):
            pass

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    code = build_start_code(
        "j-3", name="nb", argv=["{python}", "-u", "{dir}/notebook.py", "{dir}/cells.json"],
        env={}, files={"notebook.py": "print('nb')\n", "cells.json": "[]"},
        jobs_dir=str(tmp_path).replace("\\", "/"),
    )
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(code, "<start>", "exec"), {})
    assert (tmp_path / "j-3" / "notebook.py").read_text() == "print('nb')\n"
    assert (tmp_path / "j-3" / "cells.json").read_text() == "[]"
    assert (tmp_path / "j-3" / "runner.py").exists()
