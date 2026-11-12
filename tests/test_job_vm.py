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

"""The VM-side job runner and the colab_feedback helper (phase B).

Both are plain-stdlib sources the CLI writes to the runtime. These tests run
them locally with a real child process, on every OS the CLI supports.
"""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from colab_cli.job_vm import FEEDBACK_SOURCE, RUNNER_SOURCE


def _ns(source):
    ns = {"__name__": "colab_job_test"}
    exec(compile(source, "<vm>", "exec"), ns)
    return ns


@pytest.mark.parametrize(
    "line, expected",
    [
        (" 45%|████▌     | 450/1000 [00:10<00:12, 44.8it/s]",
         {"percent": 45, "n": 450, "total": 1000, "eta_s": 12}),
        ("Epoch 1:  7%|▋         | 70/1000 [01:02<1:23:45,  1.1it/s, loss=0.4]",
         {"percent": 7, "n": 70, "total": 1000, "eta_s": 5025}),
        ("100%|██████████| 3/3 [00:01<00:00,  2.0it/s]",
         {"percent": 100, "n": 3, "total": 3, "eta_s": 0}),
        (" 12%|#2        | 12/? [00:01<?, ?it/s]",
         {"percent": 12, "n": None, "total": None, "eta_s": None}),
    ],
)
def test_parse_progress(line, expected):
    assert _ns(RUNNER_SOURCE)["parse_progress"](line) == expected


def test_parse_progress_ignores_plain_percent_text():
    assert _ns(RUNNER_SOURCE)["parse_progress"]("accuracy 45% so far") is None


def _make_job(tmp_path, body, job_id="j1"):
    d = tmp_path / job_id
    (d / "lib").mkdir(parents=True)
    (d / "runner.py").write_text(RUNNER_SOURCE, encoding="utf-8")
    (d / "lib" / "colab_feedback.py").write_text(FEEDBACK_SOURCE, encoding="utf-8")
    (d / "script.py").write_text(textwrap.dedent(body), encoding="utf-8")
    spec = {"id": job_id, "name": "t", "argv": [sys.executable, "-u", str(d / "script.py")], "cwd": str(tmp_path)}
    (d / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return d


def _start(d, **extra_env):
    env = dict(os.environ)
    env.update(
        COLAB_JOB_DIR=str(d),
        PYTHONPATH=str(d / "lib"),
        COLAB_JOB_POLL="0.05",
        COLAB_JOB_STATUS_EVERY="0.1",
        COLAB_JOB_GRACE="2",
        **extra_env,
    )
    return subprocess.Popen(
        [sys.executable, "-u", str(d / "runner.py"), str(d)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _status(d):
    return json.loads((d / "status.json").read_text(encoding="utf-8"))


def test_successful_job_records_output_progress_and_metrics(tmp_path):
    d = _make_job(tmp_path, """
        import sys
        from colab_feedback import report, note
        print("hello")
        for i in range(1, 4):
            sys.stdout.write(f"\\r{i * 33}%|###       | {i}/3 [00:01<00:02,  1.0it/s]")
            sys.stdout.flush()
            report(step=i, loss=1.0 / i)
        print()
        note("done training")
        print("ünnep")
    """)

    p = _start(d)
    assert p.wait(timeout=30) == 0, p.stderr.read()

    st = _status(d)
    assert st["state"] == "succeeded"
    assert st["exit_code"] == 0
    assert st["ended_at"] >= st["started_at"]
    assert st["progress"]["n"] == 3 and st["progress"]["total"] == 3
    log = (d / "output.log").read_bytes().decode("utf-8")
    assert "hello" in log and "ünnep" in log
    tail = (d / "tail.txt").read_text(encoding="utf-8").splitlines()
    assert tail[0] == "hello" and tail[-1] == "ünnep"
    rows = [json.loads(x) for x in (d / "metrics.jsonl").read_text().splitlines()]
    assert [r["step"] for r in rows] == [1, 2, 3]
    assert rows[-1]["loss"] == pytest.approx(1 / 3)
    notes = [json.loads(x) for x in (d / "notes.jsonl").read_text().splitlines()]
    assert notes[0]["text"] == "done training"


def test_failing_job_keeps_the_traceback_in_the_tail(tmp_path):
    d = _make_job(tmp_path, """
        print("start")
        raise ValueError("bad config")
    """)

    _start(d).wait(timeout=30)

    st = _status(d)
    assert st["state"] == "failed"
    assert st["exit_code"] == 1
    tail = (d / "tail.txt").read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in tail
    assert tail.rstrip().endswith("ValueError: bad config")


def test_stop_file_stops_the_job(tmp_path):
    d = _make_job(tmp_path, """
        import time
        print("looping", flush=True)
        while True:
            time.sleep(0.05)
    """)

    p = _start(d)
    deadline = time.time() + 20
    while time.time() < deadline:
        if (d / "status.json").exists() and _status(d)["state"] == "running":
            break
        time.sleep(0.05)
    (d / "stop").write_text("")
    assert p.wait(timeout=30) == 0

    st = _status(d)
    assert st["state"] == "stopped"
    assert st["exit_code"] is not None


def test_feedback_helper_is_a_no_op_outside_a_job(tmp_path, monkeypatch):
    monkeypatch.delenv("COLAB_JOB_DIR", raising=False)
    ns = _ns(FEEDBACK_SOURCE)
    ns["report"](step=1, loss=0.5)
    ns["note"]("x")
    assert list(tmp_path.iterdir()) == []


def test_sources_are_plain_python_without_cli_imports():
    for src in (RUNNER_SOURCE, FEEDBACK_SOURCE):
        compile(src, "<vm>", "exec")
        assert "colab_cli" not in src
