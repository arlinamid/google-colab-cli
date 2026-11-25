---
log:
2026-11-25: Chunks are 16 MiB instead of upstream #272's 64 MiB, and a chunk whose connection drops (`requests.ConnectionError`, which includes `SSLError`, or `Timeout`) is sent again up to `UPLOAD_RETRIES` (2) times (`_put_chunk_retrying`). Measured live at about 2 MiB/s: 2 of 9 single requests of 56-64 MiB ended in `SSLEOFError` partway (a slow 45 s small body passed, so it is not a time limit, and sizes that failed once passed later), none of the 16 MiB ones did, and upstream's version failed a 100 MiB upload on its first chunk. Chunk 1 and single-request uploads are resent as they are (they replace the file); a later chunk is resent only when `GET ...?content=0` reports the size before it (not stored), is skipped when it reports the size after it (only the response was lost), and otherwise the original error ends in `IncompleteUploadError`. Live: a 100 MiB file went up in 55 s and came back with the same SHA-256.
2026-11-25: Took upstream PR #272 (chunked uploads, by yschoi9733) before Google merged it. `colab upload`, `upload -r` (`upload_dir`), `colab edit` and the tray's file manager (which runs `colab upload`) all go through `ContentsClient.upload`; `refresh_session` is passed where the CLI has a session name.
2026-11-25: Merged upstream #279: every Contents API request (`ContentsClient._request`, on this fork's pooled `requests.Session`) has a `(30, 300)` s `(connect, read)` timeout, so a dropped connection fails instead of hanging `upload`/`download`/`ls`/`rm`/`edit` and the tray file manager (discussion #252).
2026-11-20: `upload`/`download --progress` prints `[colab] progress DONE/TOTAL PATH` after each file (TOTAL is `?` for a recursive download, which does not count first); the tray's file manager reads it for its progress bar. `upload/download --via ssh` kill their scp when they get SIGTERM, so a cancel does not leave scp copying.
2026-11-20: For the tray's file manager: `colab ls --json` (one object: path, items with name/type/size/last_modified, folders first; an error is JSON too), `colab mkdir`, `colab rm -r` (`ContentsClient.rm_tree`, children first, since the contents API refuses a folder that is not empty), and `--via ssh [-i KEY]` on `upload`/`download`, which copies with OpenSSH scp through `colab ssh --proxy-mode` (`ssh.scp_transfer`) using the default key setting. Tested live on an attached web UI runtime.
2026-11-20: Took upstream PR #267 (`colab upload -r` / `colab download -r`, by Koji Shukawa) before Google merged it, with its review comments applied: no `ntpath` import (a drive-prefixed entry name is still refused on Windows, through `os.path.splitdrive`), simpler `posixpath.join` calls, `_is_local_symlink` so a destination given as `link/` is still seen as a symlink on POSIX, and HTTP errors without the URL (the fork's message, which hides the runtime proxy token). Tested live on a Colab web UI runtime: a tree with subfolders and an empty folder uploaded and downloaded back identical.
2026-11-19: Added `-r`/`--recursive` support to `colab upload` and `colab download` via `ContentsClient.mkdir`, `_ensure_dir`, `upload_dir`, and `download_dir` (preserving empty directories, skipping symlinks on upload, handling single-file `download -r` targets, normalizing remote paths, and enforcing entry-name, canonical local-symlink/containment, and recursion-depth checks). Reused a persistent `requests.Session` in `ContentsClient` with `close()`/context-manager cleanup (reopening a fresh client after `$EDITOR` in `colab edit`) for HTTP keep-alive, and redacted `colab-runtime-proxy-token` query params from `HTTPError` messages.
2026-11-12: Errors no longer show the runtime proxy token: `ContentsClient` reports an HTTP error as status and path (requests put the full URL, token included, in its message) and the file commands print errors through `common.redact_secrets`. Under Git Bash on Windows, remote paths that MSYS rewrote (`/content` -> `C:/Program Files/Git/content`) are turned back by `utils.undo_msys_path`. A failing editor in `colab edit` prints `[colab] ...: Editing failed` and exits 1 instead of a traceback (Typer vendors its own Click and does not handle `click.ClickException`).
2026-11-23: `colab upload` sends a file that does not fit in one request (about 75 MiB) in 64 MiB chunks with the Jupyter Contents API chunked protocol. Colab runtimes reject request bodies over 100 MiB with HTTP 400, so such uploads used to fail. This replaces the planned kernel-streaming fallback.
---

# Design: File Management (`ls`, `rm`, `upload`, `download`, `edit`)

## Overview
File management on the Colab VM will be implemented using the Jupyter Contents API.

## Approach

### 1. Listing Files (`colab ls`)
- **API**: `GET /api/contents/<path>` (as seen in HAR L68181).
- **Parameters**: 
    - `authuser`: 0
    - `colab-runtime-proxy-token`: <session_token>
- **Response**: JSON with `content` field containing an array of directory entries.
- **Display**: Pretty-print the list (similar to `ls -F` or a formatted table).

### 2. Uploading Files (`colab upload`)
- **API**: `PUT /api/contents/<remote_path>` (as seen in HAR).
- **Payload**: JSON body:
    ```json
    {
      "name": "filename.txt",
      "path": "path/filename.txt",
      "type": "file",
      "format": "text",
      "content": "..."
    }
    ```
- **Base64 Encoding**: Use `format: base64` for binary files.
- **Progress**: Implement a simple progress bar for large uploads by chunking or providing status updates.
- **Recursive (`-r` / `--recursive`)**: Walks a local directory top-down (`followlinks=False`), creates the remote directory (and any missing parent directories) with `PUT /api/contents/<path>` (`{"type": "directory"}`), and uploads files using POSIX `/` paths. Uploading a directory without `-r` exits non-zero with a `-r/--recursive` hint.

### 3. Downloading Files (`colab download`)
- **API**: `GET /api/contents/<remote_path>?content=1` (as seen in HAR).
- **Response**: JSON with `content` field.
- **Handling**: Decodes content based on `format` (`base64`, `json`, or plain `text`) and saves it locally.
- **Recursive (`-r` / `--recursive`)**: Walks remote directories returned by the Contents API, creates matching local directories (including empty directories), validates entry names against path traversal, and downloads nested files/notebooks (or falls back to downloading a single file if `remote_path` is not a directory). Non-recursive `download` raises `IsADirectoryError` with a `-r/--recursive` hint when targeting a directory.
- **Timeouts**: Every Contents API request uses a `(connect, read)` timeout of `(30, 300)` seconds. The read timeout bounds the wait between bytes, not the whole transfer, so large files still finish while a dropped connection fails with an error instead of hanging `ls`, `rm`, `upload`, `download`, or `edit` forever.

### 4. Deleting Files (`colab rm`)
- **API**: `DELETE /api/contents/<remote_path>`.

### 5. Editing Files (`colab edit`)
- **Approach**: Combines downloading the remote file, opening it in the user's `$EDITOR` locally, and subsequently uploading the changed file if modifications were made.
- **State tracking**: Uses a SHA-256 hash to track file changes securely and deterministically between before and after the editor is invoked.
- **Fallbacks**: Creates an empty local temporary file if the target file on the Colab runtime doesn't exist yet, essentially acting like `touch`.
- **Editor failure**: a non-zero exit or a missing editor prints `[colab] <editor>: Editing failed` and exits 1; nothing is uploaded.

## Implementation Details
- **Base URL**: The backend URL obtained during session assignment.
- **Proxy Token**: The `colab-runtime-proxy-token` is required for each request.
- **Error Handling**: Handle 404 (not found) and 403 (unauthorized). Other HTTP errors are reported as `<status> <reason> for /api/contents/<path>`, never with the request URL, whose query carries the proxy token; printed errors also pass through `redact_secrets`.
- **Git Bash paths**: MSYS rewrites POSIX-looking arguments for native programs. On Windows with `MSYSTEM` set, `ls`, `rm`, `upload`, `download` and `edit` turn a remote path inside the MSYS root (`cygpath -m /`, else `EXEPATH`) back into a POSIX path. Linux and macOS are unaffected.
- **Large Files**: Colab runtimes reject request bodies over 100 MiB with HTTP 400, and uploads are base64-encoded inside JSON (4/3 of the file size). `fits_in_one_request` computes the exact JSON body size for the path. A file that fits (about 75 MiB, depending on the path length) is still sent in one `"chunk": 1` request, as before, so every upload that used to work behaves the same. A larger file is sent in chunks of `UPLOAD_CHUNK_SIZE` (16 MiB, about 21 MiB once encoded; upstream uses 64 MiB) with the Jupyter Contents API chunked protocol, the same one JupyterLab's file browser uses: chunk `1` replaces the file in one save, later chunks are appended, and `-1` marks the last chunk.
    - A lone `-1` chunk would be appended to an existing file, so it is only ever sent after chunk `1`.
    - A request whose connection drops (an SSL EOF partway through a long upload, or a timeout) is sent again, up to `UPLOAD_RETRIES` (2) times. Chunk `1` and single-request uploads replace the file, so they are simply resent. Before resending a later chunk the client reads the remote size (`?content=0`): the size before the chunk means it was not stored, the size after it means only the response was lost and the chunk is not appended twice, and any other size stops the upload.
    - If any chunk fails, the remote file may be partly written (even a failed chunk `1` may have been stored if only the response was lost). `IncompleteUploadError` says so, with how many chunks were sent and the cause (for `requests` errors only the type, HTTP status and reason, because their text includes the URL with the proxy token), and the upload has to be run again. A local read error after chunk `1` is reported the same way. If the runtime refuses chunk `1` (404 or another 4xx), nothing was written and that error is raised unchanged. An upload interrupted with Ctrl-C after chunk `1` also leaves a partial file. A single-request upload is never partial: it either replaces the file or leaves it as it was.
    - A chunked upload can outlast the runtime proxy token. `colab upload` and `colab edit` pass `refresh_session` (`State.get_session`, which renews the token when it is close to expiry) to `ContentsClient`, which calls it before every chunk after the first. If that refresh fails (including a `sys.exit` from the credential code), the upload is reported as incomplete too.
    - Chunks are written straight to `remote_path` rather than to a temporary file that is renamed at the end. The Contents API refuses to rename onto an existing path (HTTP 409), so a rename would need a delete first. If the connection drops between the two calls, the old file is deleted and the new data is left under the temporary name, and deleting and renaming replaces a symlinked destination with a regular file instead of writing through it.
    - The stored size is not compared with the local size after the upload: the Contents API reports `os.lstat` sizes, so an upload through a symlinked destination would look incomplete.

## Testing Strategy
TDD is mandatory for all file management features.

### 1. Mock Contents API
- **Test Case**: Verify `colab ls` correctly parses a Jupyter `contents` JSON response with `type: directory` and `type: file`.
- **Test Case**: Verify `colab upload` correctly base64-encodes a binary local file for the `PUT` payload.
- **Test Case**: Verify `colab download` correctly decodes the `content` field from the `GET` response and saves it locally.
- **Test Case**: Verify `colab edit` safely handles when a file is or isn't modified.
- **Test Case**: Verify `colab edit` securely opens a system editor safely through mocks without hanging the testing environment.

### 2. Error Cases
- **Test Case**: Verify 404 responses are correctly caught and presented as a "File not found" error to the user.
- **Test Case**: Verify a file larger than one chunk is sent as chunks `1, 2, ..., -1`, with no empty trailing chunk, and a file that fits in one chunk as a single `"chunk": 1` request.
- **Test Case**: Verify a failed single-request upload raises the original error, and a failed chunk of a multi-chunk upload raises `IncompleteUploadError` without the proxy token in its message or exception chain.
- **Test Case**: Verify a failed `colab upload` names the error without the proxy token, and a failed token refresh during a chunked upload raises `IncompleteUploadError`.
