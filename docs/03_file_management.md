---
log:
2026-11-20: Took upstream PR #267 (`colab upload -r` / `colab download -r`, by Koji Shukawa) before Google merged it, with its review comments applied: no `ntpath` import (a drive-prefixed entry name is still refused on Windows, through `os.path.splitdrive`), simpler `posixpath.join` calls, `_is_local_symlink` so a destination given as `link/` is still seen as a symlink on POSIX, and HTTP errors without the URL (the fork's message, which hides the runtime proxy token). Tested live on a Colab web UI runtime: a tree with subfolders and an empty folder uploaded and downloaded back identical.
2026-11-19: Added `-r`/`--recursive` support to `colab upload` and `colab download` via `ContentsClient.mkdir`, `_ensure_dir`, `upload_dir`, and `download_dir` (preserving empty directories, skipping symlinks on upload, handling single-file `download -r` targets, normalizing remote paths, and enforcing entry-name, canonical local-symlink/containment, and recursion-depth checks). Reused a persistent `requests.Session` in `ContentsClient` with `close()`/context-manager cleanup (reopening a fresh client after `$EDITOR` in `colab edit`) for HTTP keep-alive, and redacted `colab-runtime-proxy-token` query params from `HTTPError` messages.
2026-11-12: Errors no longer show the runtime proxy token: `ContentsClient` reports an HTTP error as status and path (requests put the full URL, token included, in its message) and the file commands print errors through `common.redact_secrets`. Under Git Bash on Windows, remote paths that MSYS rewrote (`/content` -> `C:/Program Files/Git/content`) are turned back by `utils.undo_msys_path`. A failing editor in `colab edit` prints `[colab] ...: Editing failed` and exits 1 instead of a traceback (Typer vendors its own Click and does not handle `click.ClickException`).
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
- **Large Files**: The Contents API might have limitations for very large files. If so, we'll implement a fallback via the kernel (streaming chunks).

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
- **Test Case**: Verify correct handling of large file uploads exceeding API limits via kernel streaming.
