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

import click
import hashlib
import os
import shutil
import subprocess
import tempfile
import typer
from typing import Optional
from typing_extensions import Annotated

from colab_cli.contents import ContentsClient
from colab_cli.terminal import is_windows, split_windows_command_line


def ls(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to list")] = "content",
):
    """List files in a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.get_session(name)
    contents = ContentsClient(s)
    try:
        data = contents.list_dir(path)
        state.history.log_event(name, "file_operation", {"op": "ls", "path": path})
        if data.get("type") == "directory":
            items = data.get("content", [])
            for item in sorted(
                items, key=lambda x: (x.get("type") != "directory", x.get("name"))
            ):
                suffix = "/" if item.get("type") == "directory" else ""
                typer.echo(f"{item.get('name')}{suffix}")
        else:
            typer.echo(data.get("name"))
    except Exception as e:
        typer.echo(f"[colab] Error: {e}")
        raise typer.Exit(1)


def rm(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to remove")] = ...,
):
    """Remove a remote file"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.get_session(name)
    contents = ContentsClient(s)
    try:
        contents.rm(path)
        state.history.log_event(name, "file_operation", {"op": "rm", "path": path})
        typer.echo(f"[colab] Deleted {path}")
    except Exception as e:
        typer.echo(f"[colab] Error: {e}")
        raise typer.Exit(1)


def upload(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    local_path: Annotated[str, typer.Argument(help="Local file to upload")] = ...,
    remote_path: Annotated[str, typer.Argument(help="Remote path to upload to")] = ...,
):
    """Upload a file to a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.get_session(name)
    if not os.path.isfile(local_path):
        typer.echo(f"[colab] Local file '{local_path}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        contents.upload(local_path, remote_path)
        state.history.log_event(
            name,
            "file_operation",
            {"op": "upload", "local": local_path, "remote": remote_path},
        )
        typer.echo(f"[colab] Uploaded '{local_path}' to '{remote_path}'")
    except Exception as e:
        typer.echo(f"[colab] Upload failed: {e}")
        raise typer.Exit(1)


def download(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    remote_path: Annotated[
        str, typer.Argument(help="Remote path to download from")
    ] = ...,
    local_path: Annotated[
        str, typer.Argument(help="Local path to save the file")
    ] = ...,
):
    """Download a file from a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.get_session(name)
    contents = ContentsClient(s)
    try:
        contents.download(remote_path, local_path)
        state.history.log_event(
            name,
            "file_operation",
            {"op": "download", "remote": remote_path, "local": local_path},
        )
        typer.echo(f"[colab] Downloaded '{remote_path}' to '{local_path}'")
    except Exception as e:
        typer.echo(f"[colab] Download failed: {e}")
        raise typer.Exit(1)


def _launch_editor(path: str) -> None:
    """Open ``path`` in the user's editor and wait for it to exit.

    click.edit splits ``$VISUAL``/``$EDITOR`` with POSIX ``shlex`` on every
    platform, which drops the backslashes of an unquoted Windows path such as
    ``C:\\tools\\edit.exe`` (pallets/click#3840). On Windows, when one of
    those variables is set, split it with the Windows rules and start the
    editor here. Everything else, including click's own default editor,
    still goes through click.edit.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not (is_windows() and editor):
        click.edit(filename=path)
        return

    argv = split_windows_command_line(editor)
    if not argv:
        click.edit(filename=path)
        return
    # CreateProcess only appends .exe; which() also finds .cmd/.bat
    # launchers such as VS Code's `code`.
    argv[0] = shutil.which(argv[0]) or argv[0]
    try:
        returncode = subprocess.call(argv + [path])
    except OSError as e:
        raise click.ClickException(f"{editor}: Editing failed: {e}") from e
    if returncode != 0:
        raise click.ClickException(f"{editor}: Editing failed")


def edit(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    remote_path: Annotated[str, typer.Argument(help="Remote path to edit")] = ...,
):
    """Edit a file on a running Colab session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.get_session(name)

    contents = ContentsClient(s)

    def get_file_hash(path):
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    # A path inside a temporary directory, not NamedTemporaryFile: on Windows
    # an open NamedTemporaryFile cannot be opened a second time, so the
    # download, the hash and the editor would all fail with PermissionError.
    # Keeping the remote basename also gives the editor the right extension.
    with tempfile.TemporaryDirectory(prefix="colab-edit-") as tmpdir:
        local_path = os.path.join(
            tmpdir, os.path.basename(remote_path.rstrip("/")) or "file"
        )

        try:
            contents.download(remote_path, local_path)
        except Exception:
            # If download fails, assume file doesn't exist and start empty
            pass
        if not os.path.exists(local_path):
            open(local_path, "wb").close()

        hash_before = get_file_hash(local_path)

        _launch_editor(local_path)

        hash_after = get_file_hash(local_path)

        if hash_after != hash_before:
            contents.upload(local_path, remote_path)
            state.history.log_event(
                name,
                "file_operation",
                {"op": "edit", "remote": remote_path},
            )
            typer.echo(f"[colab] Edited and uploaded '{remote_path}'")
        else:
            typer.echo(f"[colab] No changes made to '{remote_path}'")


def register(app: typer.Typer):
    app.command()(ls)
    app.command()(rm)
    app.command()(upload)
    app.command()(download)
    app.command()(edit)
