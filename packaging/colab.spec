# -*- mode: python ; coding: utf-8 -*-
# The standalone `colab` binary. Run it through packaging/build_binary.py,
# which sets COLAB_BINARY_NAME; what is left out is defined there.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

sys.path.insert(0, SPECPATH)
from build_binary import EXCLUDED_MODULES, ROOT, keep_file  # noqa: E402

datas = [
    # README and SKILL are mapped into the package only when the wheel is
    # built (pyproject.toml).
    (os.path.join(ROOT, "README.md"), "colab_cli"),
    (os.path.join(ROOT, "skills", "colab-operator", "SKILL.md"), "colab_cli"),
]
# Package data: the OAuth client config and the tray's logo.
datas += collect_data_files("colab_cli")
# nbformat validates notebooks against bundled JSON schemas.
datas += collect_data_files("nbformat")
# `colab version` and the update check read the package metadata.
datas += copy_metadata("google-colab-cli")
# The OAuth token is kept with keyring, whose backends are discovered at run time.
datas += copy_metadata("keyring")
hiddenimports = collect_submodules("keyring.backends")

a = Analysis(
    [os.path.join(ROOT, "packaging", "colab_entry.py")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=EXCLUDED_MODULES,
)
a.binaries = [entry for entry in a.binaries if keep_file(entry[0])]
a.datas = [entry for entry in a.datas if keep_file(entry[0])]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=os.environ.get("COLAB_BINARY_NAME", "colab"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # The tray's logo as an .ico, made by build_binary.py on Windows.
    icon=os.environ.get("COLAB_BINARY_ICON") or None,
)
