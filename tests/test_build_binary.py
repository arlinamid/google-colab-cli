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


def _ico_entries(data: bytes):
    """(width, payload) for each image of an .ico file."""
    import struct

    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    entries = []
    for i in range(count):
        w, h, _colors, _res, planes, bpp, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        assert w == h and planes == 1 and bpp == 32
        entries.append((w or 256, data[offset:offset + size]))
    return entries


def test_ico_bytes_holds_each_png_with_its_size():
    pngs = {16: b"\x89PNG-16", 256: b"\x89PNG-256-longer"}

    entries = _ico_entries(build_binary.ico_bytes(pngs))

    assert entries == [(16, b"\x89PNG-16"), (256, b"\x89PNG-256-longer")]


def test_write_icon_draws_the_tray_logo_at_every_size(tmp_path):
    pytest.importorskip("PySide6.QtSvg", exc_type=ImportError)
    from PySide6.QtGui import QImage

    out = tmp_path / "colab.ico"
    build_binary.write_icon(build_binary.ICON_SVG, str(out))

    entries = _ico_entries(out.read_bytes())
    assert [w for w, _ in entries] == list(build_binary.ICON_SIZES)
    for w, png in entries:
        img = QImage.fromData(png, "PNG")
        assert (img.width(), img.height()) == (w, w)
    big = QImage.fromData(entries[-1][1], "PNG")
    assert big.pixelColor(2, 2).alpha() == 0            # transparent corner
    assert big.pixelColor(244, 128).alpha() == 255      # the ring on the right


def test_the_logo_is_the_forks():
    """The fork's logo: Colab's C, and a teal ring instead of the orange one."""
    with open(build_binary.ICON_SVG, encoding="utf-8") as f:
        svg = f.read()
    assert "#16A6B6" in svg and "#087783" in svg


def test_the_spec_sets_the_icon():
    with open(os.path.join(ROOT, "packaging", "colab.spec"), encoding="utf-8") as f:
        assert "COLAB_BINARY_ICON" in f.read()
