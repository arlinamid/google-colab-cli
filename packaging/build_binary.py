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

"""Build the standalone `colab` binary for this OS with PyInstaller.

Usage (from the repository root). Install the package with the tray and
without the dev tools, so neither pytest nor IPython gets into the binary:

    uv run --locked --exact --no-dev --extra tray --with pyinstaller \\
        python packaging/build_binary.py [NAME]

Writes dist/bin/NAME (dist/bin/NAME.exe on Windows); NAME defaults to colab.
packaging/colab.spec holds the build; this module also holds what it leaves
out (EXCLUDED_MODULES, keep_file), so tests can check them without PyInstaller.
"""

import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Packages the CLI never imports in the binary. jupyter-kernel-client pulls in
# jupyter-mimetypes, which imports pyarrow, pandas and IPython only to move
# variables between kernels (the CLI does not); pyarrow alone was more than
# half of the binary. pytest and jedi come from the dev tools.
EXCLUDED_MODULES = [
    "pyarrow",
    "pandas",
    "numpy",
    "IPython",
    "jedi",
    "parso",
    "pytest",
    "_pytest",
    "PySide6.QtNetwork",
]

# Qt files the tray does not need: the software OpenGL renderer (20 MB; the
# tray draws with QPainter), Qt's own translations (the tray has its own
# strings), and the network module with its TLS plugins.
_DROPPED_FILES = [
    r"(^|/)opengl32sw\.dll$",
    r"^PySide6/translations/",
    r"(^|/)(lib)?Qt6?Network",
    r"^PySide6/plugins/tls/",
]


def keep_file(dest: str) -> bool:
    """False for a collected file (path inside the binary) left out of it."""
    dest = dest.replace("\\", "/")
    return not any(re.search(p, dest) for p in _DROPPED_FILES)


# The tray's logo is also the Windows binary's icon (Explorer, taskbar).
ICON_SVG = os.path.join(ROOT, "src", "colab_cli", "tray", "colab.svg")
ICON_SIZES = (16, 24, 32, 48, 64, 256)


def ico_bytes(pngs: dict) -> bytes:
    """An .ico file holding one PNG image per size ({size: png bytes})."""
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    entries, offset = b"", 6 + 16 * len(sizes)
    for size in sizes:
        side = 0 if size >= 256 else size  # 0 stands for 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(pngs[size]), offset)
        offset += len(pngs[size])
    return header + entries + b"".join(pngs[s] for s in sizes)


def write_icon(svg_path: str, out_path: str) -> None:
    """Draw the logo centred on a transparent square at each of ICON_SIZES
    with Qt (the build has the tray extra) and save them as one .ico."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    with open(svg_path, "rb") as f:
        renderer = QSvgRenderer(QByteArray(f.read()))
    box = renderer.viewBoxF()
    pngs = {}
    for size in ICON_SIZES:
        img = QImage(size, size, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        height = size * box.height() / box.width()
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter, QRectF(0, (size - height) / 2, size, height))
        painter.end()
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        pngs[size] = bytes(buf.data())
    with open(out_path, "wb") as f:
        f.write(ico_bytes(pngs))


def main() -> None:
    import PyInstaller.__main__

    name = sys.argv[1] if len(sys.argv) > 1 else "colab"
    os.environ["COLAB_BINARY_NAME"] = name
    if sys.platform == "win32":
        # PyInstaller reads only .ico on Windows; the other binaries have no
        # icon of their own (a macOS icon needs an .app bundle).
        icon = os.path.join(ROOT, "build", "colab.ico")
        os.makedirs(os.path.dirname(icon), exist_ok=True)
        write_icon(ICON_SVG, icon)
        os.environ["COLAB_BINARY_ICON"] = icon
    PyInstaller.__main__.run(
        [
            os.path.join(ROOT, "packaging", "colab.spec"),
            "--distpath",
            os.path.join(ROOT, "dist", "bin"),
            "--workpath",
            os.path.join(ROOT, "build", "pyinstaller"),
            "--noconfirm",
            "--clean",
        ]
    )


if __name__ == "__main__":
    main()
