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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import typer
from typing import Optional
from typing_extensions import Annotated

from colab_cli.common import redact_secrets
from colab_cli.contents import ContentsClient
from colab_cli.terminal import is_windows, split_windows_command_line
from colab_cli.utils import undo_msys_path


def ls(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to list")] = "content",
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="One JSON object: path and items (name, type, size, last_modified).",
        ),
    ] = False,
):
    """List files in a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    path = undo_msys_path(path)
    s = state.get_session(name)
    contents = ContentsClient(s)
    try:
        data = contents.list_dir(path)
        state.history.log_event(name, "file_operation", {"op": "ls", "path": path})
        if as_json:
            typer.echo(json.dumps(_listing(path, data)))
            return
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
        if as_json:
            typer.echo(json.dumps({"path": path, "error": redact_secrets(str(e))}))
        else:
            typer.echo(f"[colab] Error: {redact_secrets(str(e))}")
        raise typer.Exit(1)
    finally:
        contents.close()


def _listing(path: str, data: dict) -> dict:
    """`ls --json`: folders first, then by name, as for the plain listing."""
    if data.get("type") != "directory":
        data = {"content": [data]}
    items = [
        {
            "name": item.get("name"),
            "type": item.get("type"),
            "size": item.get("size"),
            "last_modified": item.get("last_modified"),
        }
        for item in data.get("content") or []
    ]
    items.sort(key=lambda i: (i["type"] != "directory", i["name"] or ""))
    return {"path": path, "items": items}


def mkdir(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote folder to create")] = ...,
):
    """Create a remote folder"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    path = undo_msys_path(path)
    s = state.get_session(name)
    contents = ContentsClient(s)
    try:
        contents.mkdir(path)
        state.history.log_event(name, "file_operation", {"op": "mkdir", "path": path})
        typer.echo(f"[colab] Created {path}")
    except Exception as e:
        typer.echo(f"[colab] Error: {redact_secrets(str(e))}")
        raise typer.Exit(1)
    finally:
        contents.close()


def rm(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to remove")] = ...,
    recursive: Annotated[
        bool,
        typer.Option("-r", "--recursive", help="Remove a folder and everything in it"),
    ] = False,
):
    """Remove a remote file, or a folder with -r"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    path = undo_msys_path(path)
    s = state.get_session(name)
    contents = ContentsClient(s)
    try:
        if recursive:
            contents.rm_tree(path)
        else:
            contents.rm(path)
        state.history.log_event(
            name, "file_operation", {"op": "rm", "path": path, "recursive": recursive}
        )
        typer.echo(f"[colab] Deleted {path}")
    except Exception as e:
        typer.echo(f"[colab] Error: {redact_secrets(str(e))}")
        raise typer.Exit(1)
    finally:
        contents.close()


def upload(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    local_path: Annotated[
        str, typer.Argument(help="Local file or directory to upload")
    ] = ...,
    remote_path: Annotated[str, typer.Argument(help="Remote path to upload to")] = ...,
    recursive: Annotated[
        bool,
        typer.Option("-r", "--recursive", help="Recursively upload a directory"),
    ] = False,
    via: Annotated[
        str,
        typer.Option(
            "--via",
            help="contents (Jupyter contents API, the default) or ssh (scp; needs an SSH key).",
        ),
    ] = "contents",
    identity: Annotated[
        Optional[str],
        typer.Option("--identity", "-i", help="With --via ssh: the SSH key, as for `colab ssh`."),
    ] = None,
    progress: Annotated[
        bool,
        typer.Option("--progress", help="Print a line per copied file: [colab] progress DONE/TOTAL PATH."),
    ] = False,
):
    """Upload a file or directory to a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    remote_path = undo_msys_path(remote_path)
    s = state.get_session(name)
    is_dir = os.path.isdir(local_path)
    if is_dir and not recursive:
        typer.echo(
            f"[colab] '{local_path}' is a directory. Use -r/--recursive to upload directories."
        )
        raise typer.Exit(1)
    if not is_dir and not os.path.isfile(local_path):
        label = "path" if recursive else "file"
        typer.echo(f"[colab] Local {label} '{local_path}' not found.")
        raise typer.Exit(1)
    if _check_via(via) == "ssh":
        _transfer_via_ssh(state, name, s, identity, local_path, remote_path, upload=True, recursive=is_dir)
        return
    contents = ContentsClient(s)
    try:
        if is_dir:
            count = contents.upload_dir(local_path, remote_path, **_progress_kwargs(progress))
            state.history.log_event(
                name,
                "file_operation",
                {
                    "op": "upload",
                    "local": local_path,
                    "remote": remote_path,
                    "recursive": True,
                    "count": count,
                },
            )
            typer.echo(
                f"[colab] Uploaded directory '{local_path}' ({count} file(s)) to '{remote_path}'"
            )
        else:
            contents.upload(local_path, remote_path)
            if progress:
                _progress_printer(True)(1, 1, remote_path)
            state.history.log_event(
                name,
                "file_operation",
                {"op": "upload", "local": local_path, "remote": remote_path},
            )
            typer.echo(f"[colab] Uploaded '{local_path}' to '{remote_path}'")
    except Exception as e:
        typer.echo(f"[colab] Upload failed: {redact_secrets(str(e))}")
        raise typer.Exit(1)
    finally:
        contents.close()


def download(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    remote_path: Annotated[
        str, typer.Argument(help="Remote path to download from")
    ] = ...,
    local_path: Annotated[
        str, typer.Argument(help="Local path to save the file or directory")
    ] = ...,
    recursive: Annotated[
        bool,
        typer.Option("-r", "--recursive", help="Recursively download a directory"),
    ] = False,
    via: Annotated[
        str,
        typer.Option(
            "--via",
            help="contents (Jupyter contents API, the default) or ssh (scp; needs an SSH key).",
        ),
    ] = "contents",
    identity: Annotated[
        Optional[str],
        typer.Option("--identity", "-i", help="With --via ssh: the SSH key, as for `colab ssh`."),
    ] = None,
    progress: Annotated[
        bool,
        typer.Option("--progress", help="Print a line per copied file: [colab] progress DONE/TOTAL PATH."),
    ] = False,
):
    """Download a file or directory from a session"""
    from colab_cli.common import state

    _check_via(via)
    name = state.resolve_session(session)
    remote_path = undo_msys_path(remote_path)
    s = state.get_session(name)
    if via == "ssh":
        _transfer_via_ssh(state, name, s, identity, remote_path, local_path, upload=False, recursive=recursive)
        return
    contents = ContentsClient(s)
    try:
        if recursive:
            count = contents.download_dir(remote_path, local_path, **_progress_kwargs(progress))
            state.history.log_event(
                name,
                "file_operation",
                {
                    "op": "download",
                    "remote": remote_path,
                    "local": local_path,
                    "recursive": True,
                    "count": count,
                },
            )
            typer.echo(
                f"[colab] Downloaded '{remote_path}' ({count} file(s)) to '{local_path}'"
            )
        else:
            contents.download(remote_path, local_path)
            if progress:
                _progress_printer(True)(1, 1, remote_path)
            state.history.log_event(
                name,
                "file_operation",
                {"op": "download", "remote": remote_path, "local": local_path},
            )
            typer.echo(f"[colab] Downloaded '{remote_path}' to '{local_path}'")
    except Exception as e:
        typer.echo(f"[colab] Download failed: {redact_secrets(str(e))}")
        raise typer.Exit(1)
    finally:
        contents.close()


def _progress_printer(enabled: bool):
    """With --progress, one line per file for the tray's file manager to read."""
    if not enabled:
        return None

    def printer(done: int, total, path: str) -> None:
        typer.echo(f"[colab] progress {done}/{total if total is not None else '?'} {path}")
        sys.stdout.flush()

    return printer


def _progress_kwargs(enabled: bool) -> dict:
    return {"on_file": _progress_printer(True)} if enabled else {}


def _check_via(via: str) -> str:
    if via not in ("contents", "ssh"):
        typer.echo(f"[colab] --via must be contents or ssh, not {via!r}.", err=True)
        raise typer.Exit(2)
    return via


def _transfer_via_ssh(state, name, s, identity, source, target, *, upload, recursive) -> None:
    """scp through the session's SSH bridge; exits with scp's code on failure."""
    from colab_cli.commands.ssh import scp_transfer

    code = scp_transfer(s, identity, source, target, upload=upload, recursive=recursive)
    op = "upload" if upload else "download"
    state.history.log_event(
        name,
        "file_operation",
        {"op": op, "source": source, "target": target, "via": "ssh", "recursive": recursive, "exit_code": code},
    )
    if code:
        typer.echo(f"[colab] {op.capitalize()} over ssh failed (scp exit code {code}).", err=True)
        raise typer.Exit(code)
    typer.echo(f"[colab] {'Uploaded' if upload else 'Downloaded'} '{source}' to '{target}' over ssh")


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
    remote_path = undo_msys_path(remote_path)
    s = state.get_session(name)

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

        dl_client = ContentsClient(s)
        try:
            dl_client.download(remote_path, local_path)
        except Exception:
            # If download fails, assume file doesn't exist and start empty
            pass
        finally:
            dl_client.close()
        if not os.path.exists(local_path):
            open(local_path, "wb").close()

        hash_before = get_file_hash(local_path)

        try:
            _launch_editor(local_path)
        except click.ClickException as e:
            # Typer vendors its own Click and does not handle exceptions of
            # the click package (raised here and by click.edit).
            typer.echo(f"[colab] {e.format_message()}", err=True)
            raise typer.Exit(1)

        hash_after = get_file_hash(local_path)

        if hash_after != hash_before:
            ul_client = ContentsClient(
                s, refresh_session=lambda: state.get_session(name)
            )
            try:
                ul_client.upload(local_path, remote_path)
            finally:
                ul_client.close()
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
    app.command()(mkdir)
    app.command()(upload)
    app.command()(download)
    app.command()(edit)
