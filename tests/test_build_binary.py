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

"""What goes into the standalone binary (packaging/, the release workflows)."""

import importlib.util
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_build_binary():
    spec = importlib.util.spec_from_file_location(
        "build_binary", os.path.join(ROOT, "packaging", "build_binary.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must not need PyInstaller
    return module


build_binary = _load_build_binary()


@pytest.mark.parametrize("module", [
    "pyarrow", "pandas", "numpy", "IPython", "jedi", "parso", "pytest", "_pytest",
    "PySide6.QtNetwork",
])
def test_unused_packages_are_excluded(module):
    assert module in build_binary.EXCLUDED_MODULES


@pytest.mark.parametrize("dest", [
    "PySide6/opengl32sw.dll",
    "PySide6\\opengl32sw.dll",
    "PySide6/translations/qtbase_hu.qm",
    "PySide6/Qt6Network.dll",
    "PySide6/plugins/tls/qschannelbackend.dll",
    "PySide6/Qt/lib/libQt6Network.so.6",
])
def test_unused_qt_files_are_dropped(dest):
    assert not build_binary.keep_file(dest)


@pytest.mark.parametrize("dest", [
    "PySide6/Qt6Core.dll",
    "PySide6/Qt6Gui.dll",
    "PySide6/Qt6Widgets.dll",
    "PySide6/Qt6Svg.dll",
    "PySide6/plugins/platforms/qwindows.dll",
    "PySide6/plugins/platforms/qoffscreen.dll",
    "colab_cli/tray/colab.svg",
    "python312.dll",
])
def test_needed_files_are_kept(dest):
    assert build_binary.keep_file(dest)


def test_the_cli_and_the_tray_import_none_of_the_excluded_packages():
    """An excluded package the CLI imported would fail only in the binary."""
    code = (
        "import sys\n"
        "import colab_cli.cli, colab_cli.runtime, colab_cli.jobs\n"
        "try:\n"
        "    import colab_cli.tray.app\n"
        "except ImportError:\n"
        "    pass\n"
        f"bad = [m for m in {build_binary.EXCLUDED_MODULES!r} if m in sys.modules]\n"
        "print(bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    ).stdout
    assert out.strip() == "[]"


@pytest.mark.parametrize("workflow", ["release.yml", "ci.yml"])
def test_binaries_are_built_with_the_tray_and_without_dev_tools(workflow):
    with open(os.path.join(ROOT, ".github", "workflows", workflow), encoding="utf-8") as f:
        text = f.read()
    builds = [line for line in text.splitlines() if "packaging/build_binary.py" in line and "run" in line]
    assert builds
    for line in builds:
        assert re.search(r"--no-dev\b", line), line
        assert re.search(r"--extra tray\b", line), line


@pytest.mark.parametrize("workflow", ["release.yml", "ci.yml"])
def test_the_binary_smoke_test_checks_the_tray(workflow):
    with open(os.path.join(ROOT, ".github", "workflows", workflow), encoding="utf-8") as f:
        text = f.read()
    assert 'tray --self-check' in text
