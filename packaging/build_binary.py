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

Usage (from the repository root; the package must be installed, e.g. by
`uv sync`, so its metadata and version are available):

    uv run --with pyinstaller python packaging/build_binary.py [NAME]

Writes dist/bin/NAME (dist/bin/NAME.exe on Windows); NAME defaults to colab.
"""

import os
import sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "colab"

    def data(src: str, dest: str) -> str:
        return f"{os.path.join(ROOT, src)}{os.pathsep}{dest}"

    PyInstaller.__main__.run(
        [
            os.path.join(ROOT, "packaging", "colab_entry.py"),
            "--onefile",
            "--name",
            name,
            "--distpath",
            os.path.join(ROOT, "dist", "bin"),
            "--workpath",
            os.path.join(ROOT, "build", "pyinstaller"),
            "--specpath",
            os.path.join(ROOT, "build", "pyinstaller"),
            "--noconfirm",
            "--clean",
            # Package data: OAuth client config; README and SKILL are mapped
            # into the package only when the wheel is built (pyproject.toml).
            "--collect-data",
            "colab_cli",
            "--add-data",
            data("README.md", "colab_cli"),
            "--add-data",
            data(os.path.join("skills", "colab-operator", "SKILL.md"), "colab_cli"),
            # nbformat validates notebooks against bundled JSON schemas.
            "--collect-data",
            "nbformat",
            # `colab version` and the update check read the package metadata.
            "--copy-metadata",
            "google-colab-cli",
            # The OAuth token is kept with keyring, whose backends are
            # discovered at run time.
            "--collect-submodules",
            "keyring.backends",
            "--copy-metadata",
            "keyring",
        ]
    )


if __name__ == "__main__":
    main()
