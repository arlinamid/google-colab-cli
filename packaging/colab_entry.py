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

"""Entry point for the standalone `colab` binary built with PyInstaller.

See .github/workflows/release.yml (build-binaries). The binary also runs as
its own ssh ProxyCommand (`colab ssh --proxy-mode ...`), see _proxy_command.
"""

import os

# The binary runs itself as a child (`tray --detach`, the tray's actions,
# `colab profile run`, the ssh ProxyCommand). A onefile child would reuse the
# folder this process unpacked itself into, which is deleted when this process
# exits; with this variable each child unpacks itself (PyInstaller >= 6.9).
os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"

from colab_cli.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
