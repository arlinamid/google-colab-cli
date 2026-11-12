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

"""`colab inspect`: kernel and machine state for agents (docs/08, phase C)."""

import contextlib
import io
import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.commands import inspect as inspect_cmd
from colab_cli.inspect_vm import build_inspect_code, parse_inspect_output

runner = CliRunner()


class FakeArray:
    __module__ = "numpy"

    def __init__(self):
        self.shape = (1000, 3)
        self.dtype = "float32"
        self.nbytes = 12000


class FakeTensor:
    __module__ = "torch"

    def __init__(self):
        self.shape = (8, 128)
        self.dtype = "torch.float16"
        self.device = "cuda:0"


class FakeIPython:
    def __init__(self, ns):
        self.user_ns = ns
        self.user_ns_hidden = {"In": 1}


def _run_snippet(ns, sections, baseline_dir=None):
    ns.setdefault("In", [])
    ip = FakeIPython(ns)
    code = build_inspect_code(sections, state_dir=baseline_dir or "/nonexistent")
    glb = {"get_ipython": lambda: ip}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<inspect>", "exec"), glb)
    return parse_inspect_output([{"output_type": "stream", "name": "stdout", "text": buf.getvalue()}])


def test_vars_describe_without_values():
    import os as some_module

    ns = {
        "df_like": FakeArray(),
        "model_input": FakeTensor(),
        "token": "hf_supersecretvalue",
        "items": [1, 2, 3],
        "count": 7,
        "os_mod": some_module,
        "helper": lambda x: x,
        "_private": 1,
    }
    out = _run_snippet(ns, ["vars"])

    rows = {r["name"]: r for r in out["vars"]}
    assert set(rows) == {"df_like", "model_input", "token", "items", "count"}
    assert rows["df_like"]["shape"] == [1000, 3]
    assert rows["df_like"]["dtype"] == "float32"
    assert rows["df_like"]["nbytes"] == 12000
    assert rows["model_input"]["device"] == "cuda:0"
    assert rows["items"]["len"] == 3
    assert rows["token"] == {"name": "token", "type": "str", "len": 19}
    # No value of any variable is ever printed.
    assert "supersecret" not in json.dumps(out)


def test_disk_and_memory_sections():
    out = _run_snippet({}, ["disk"])
    assert out["disk"]["total_gb"] > 0
    assert 0 <= out["disk"]["free_gb"] <= out["disk"]["total_gb"]


def test_gpu_section_without_nvidia_smi():
    out = _run_snippet({}, ["gpu"])
    # nvidia-smi is missing on most test machines: an empty list, not an error.
    assert isinstance(out["gpu"]["devices"], list)


def test_packages_baseline_then_diff(tmp_path):
    first = _run_snippet({}, ["packages"], baseline_dir=str(tmp_path))
    assert first["packages"]["baseline_created"] is True
    assert first["packages"]["added"] == []
    assert first["packages"]["count"] > 0

    # Pretend a package was installed and one changed since the baseline.
    path = tmp_path / "packages-baseline.json"
    base = json.loads(path.read_text())
    removed = base.popitem()[0]
    base["some-old-package"] = "1.0"
    path.write_text(json.dumps(base))

    second = _run_snippet({}, ["packages"], baseline_dir=str(tmp_path))
    pk = second["packages"]
    assert pk["baseline_created"] is False
    assert removed in [p["name"] for p in pk["added"]]
    assert pk["removed"] == ["some-old-package"]


def test_parse_inspect_output_ignores_other_text():
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": "noise\n"},
        {"output_type": "stream", "name": "stdout", "text": 'COLAB_INSPECT {"disk": {"free_gb": 1}}\n'},
    ]
    assert parse_inspect_output(outputs) == {"disk": {"free_gb": 1}}
    assert parse_inspect_output([]) is None


# ---- the command ---------------------------------------------------------


@pytest.fixture
def session(mock_common_state):
    s = MagicMock(url="http://u", token="t", kernel_id="k1", session_id=None)
    s.name = "s1"
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"
    return s


@pytest.fixture
def kernel(mocker, session):
    rt = mocker.patch.object(inspect_cmd, "ColabRuntime").return_value
    payload = {
        "vars": [{"name": "x", "type": "int"}],
        "gpu": {"devices": [{"name": "Tesla T4", "memory_used_mb": 300, "memory_total_mb": 15360, "utilization_pct": 0}]},
        "disk": {"total_gb": 100.0, "free_gb": 60.0},
        "memory": {"total_gb": 12.7, "available_gb": 10.1},
        "packages": {"count": 500, "baseline_created": False, "added": [{"name": "timm", "version": "1.0"}], "removed": [], "changed": []},
    }
    rt.execute_code.return_value = [
        {"output_type": "stream", "name": "stdout", "text": "COLAB_INSPECT " + json.dumps(payload) + "\n"}
    ]
    return rt


def test_inspect_json_when_the_kernel_is_idle(kernel, mocker):
    mocker.patch.object(inspect_cmd, "kernel_state", return_value="idle")

    result = runner.invoke(app, ["inspect", "-s", "s1", "--json"])

    assert result.exit_code == 0, result.output
    rec = json.loads(result.stdout)
    assert rec["schema"] == "colab.v1" and rec["type"] == "inspect"
    assert rec["kernel"] == "idle"
    assert rec["gpu"]["devices"][0]["name"] == "Tesla T4"
    code = kernel.execute_code.call_args.args[0]
    for section in ("vars", "gpu", "disk", "memory", "packages"):
        assert repr(section) in code


def test_inspect_only_selected_sections(kernel, mocker):
    mocker.patch.object(inspect_cmd, "kernel_state", return_value="idle")

    runner.invoke(app, ["inspect", "-s", "s1", "--gpu", "--json"])

    code = kernel.execute_code.call_args.args[0]
    assert "['gpu']" in code


def test_busy_kernel_is_not_queued_behind(kernel, mocker):
    mocker.patch.object(inspect_cmd, "kernel_state", return_value="busy")

    result = runner.invoke(app, ["inspect", "-s", "s1", "--json"])

    rec = json.loads(result.stdout)
    assert rec["kernel"] == "busy"
    assert rec["vars"] == {"skipped": "kernel busy"}
    kernel.execute_code.assert_not_called()


def test_files_section_uses_the_contents_api(kernel, mocker):
    mocker.patch.object(inspect_cmd, "kernel_state", return_value="busy")
    files = mocker.patch.object(inspect_cmd, "ContentsClient").return_value
    files.list_dir.return_value = {"type": "directory", "content": [
        {"name": "data", "type": "directory", "size": None},
        {"name": "model.pt", "type": "file", "size": 2048},
    ]}

    result = runner.invoke(app, ["inspect", "-s", "s1", "--files", "/content", "--json"])

    rec = json.loads(result.stdout)
    assert rec["files"]["path"] == "/content"
    assert rec["files"]["entries"] == [
        {"name": "data", "type": "directory", "size": None},
        {"name": "model.pt", "type": "file", "size": 2048},
    ]


def test_inspect_human_output(kernel, mocker):
    mocker.patch.object(inspect_cmd, "kernel_state", return_value="idle")

    result = runner.invoke(app, ["inspect", "-s", "s1"])

    assert result.exit_code == 0
    assert "Tesla T4" in result.output
    assert "60.0 GB free" in result.output
    assert "timm" in result.output


def test_kernel_state_reads_the_jupyter_api(mocker):
    from colab_cli.commands.inspect import kernel_state

    s = MagicMock(url="https://h.colab.dev", token="TOK", kernel_id="k1")
    get = mocker.patch("colab_cli.commands.inspect.requests.get")
    get.return_value.status_code = 200
    get.return_value.json.return_value = {"execution_state": "busy"}

    assert kernel_state(s) == "busy"
    url = get.call_args.args[0]
    assert url == "https://h.colab.dev/api/kernels/k1"
    assert get.call_args.kwargs["params"]["colab-runtime-proxy-token"] == "TOK"

    s.kernel_id = None
    assert kernel_state(s) == "none"
