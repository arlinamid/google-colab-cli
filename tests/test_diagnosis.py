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

"""Diagnosis rules: a fixed code and concrete next steps for an agent."""

import pytest

from colab_cli.diagnosis import diagnose, diagnose_failure


def _cmds(d):
    return [a["cmd"] for a in d["next_actions"]]


def test_every_result_has_the_same_shape():
    d = diagnose("ValueError", "bad value", session="s1")
    assert set(d) == {"code", "summary", "next_actions"}
    assert d["code"] == "unknown"
    assert d["next_actions"] == []
    assert "ValueError: bad value" in d["summary"]


@pytest.mark.parametrize(
    "evalue, package",
    [
        ("No module named 'torchvision'", "torchvision"),
        ("No module named 'cv2'", "opencv-python"),
        ("No module named 'sklearn.linear_model'", "scikit-learn"),
        ("No module named 'PIL'", "pillow"),
        ("No module named 'yaml'", "pyyaml"),
    ],
)
def test_missing_module_suggests_colab_install(evalue, package):
    d = diagnose("ModuleNotFoundError", evalue, session="demo", file="train.py")
    assert d["code"] == "missing_module"
    assert _cmds(d)[0] == f"colab install -s demo {package}"
    assert _cmds(d)[1] == "colab exec -s demo -f train.py --json"


def test_missing_module_without_a_session_has_no_command():
    """`colab run` already stopped its runtime: no -s to point at."""
    d = diagnose("ModuleNotFoundError", "No module named 'torchvision'")
    assert d["code"] == "missing_module"
    assert d["next_actions"][0]["cmd"] is None
    assert "torchvision" in d["next_actions"][0]["why"]
    assert "--keep" in d["next_actions"][0]["why"]


@pytest.mark.parametrize(
    "ename, evalue",
    [
        ("OutOfMemoryError", "CUDA out of memory. Tried to allocate 2.00 GiB"),
        ("RuntimeError", "CUDA out of memory. Tried to allocate 20.00 MiB"),
    ],
)
def test_cuda_oom(ename, evalue):
    d = diagnose(ename, evalue, session="g")
    assert d["code"] == "cuda_oom"
    assert "colab inspect -s g --gpu" in _cmds(d)
    assert any(c and "--gpu A100" in c for c in _cmds(d))


@pytest.mark.parametrize(
    "evalue",
    [
        "Torch not compiled with CUDA enabled",
        "No CUDA GPUs are available",
        "Found no NVIDIA driver on your system.",
    ],
)
def test_no_gpu(evalue):
    d = diagnose("RuntimeError", evalue, session="cpu")
    assert d["code"] == "no_gpu"
    assert any(c and "--gpu T4" in c for c in _cmds(d))


def test_disk_full():
    d = diagnose("OSError", "[Errno 28] No space left on device", session="s")
    assert d["code"] == "disk_full"
    assert "colab inspect -s s --disk" in _cmds(d)


def test_file_not_found_names_the_path():
    d = diagnose(
        "FileNotFoundError",
        "[Errno 2] No such file or directory: 'data/train.csv'",
        session="s",
    )
    assert d["code"] == "file_not_found"
    assert "data/train.csv" in d["summary"]
    assert "colab ls -s s /content" in _cmds(d)
    assert any(c and c.startswith("colab upload -s s ") for c in _cmds(d))


@pytest.mark.parametrize("ename", ["SyntaxError", "IndentationError", "TabError"])
def test_syntax_error_suggests_a_local_check(ename):
    d = diagnose(ename, "invalid syntax (1.py, line 3)", session="s", file="a.py")
    assert d["code"] == "syntax_error"
    assert "colab exec --check-only -f a.py" in _cmds(d)


def test_keyboard_interrupt_needs_nothing():
    d = diagnose("KeyboardInterrupt", "", session="s")
    assert d["code"] == "keyboard_interrupt"
    assert d["next_actions"] == []


def test_script_exit():
    d = diagnose("SystemExit", "3")
    assert d["code"] == "script_exit"
    assert "3" in d["summary"]


def test_rule_order_prefers_the_specific_cause():
    """A CUDA OOM raised as RuntimeError is not 'unknown'."""
    d = diagnose("RuntimeError", "x\nCUDA out of memory\ny", traceback="")
    assert d["code"] == "cuda_oom"


def test_matches_text_in_the_traceback_too():
    d = diagnose("RuntimeError", "failed", traceback="... CUDA out of memory ...")
    assert d["code"] == "cuda_oom"


def test_idle_timeout_failure():
    d = diagnose_failure("idle_timeout", session="s", timeout=30)
    assert d["code"] == "idle_timeout"
    assert "30" in d["summary"]
    assert any("--timeout" in a["why"] for a in d["next_actions"])


def test_session_lost_failure():
    d = diagnose_failure("session_lost", session="s")
    assert d["code"] == "session_lost"
    assert "colab status -s s" in _cmds(d)
