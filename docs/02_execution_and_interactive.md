---
log:
2026-10-07: `colab exec` notebook control and exit codes. `--cells SPEC` runs only the selected code cells, in the given order. SPEC accepts code-cell numbers (counted from 1, markdown cells not counted), ranges, `#@title` values and cell ids. Entries that look like numbers or ranges are always read as such: nbformat's random cell ids are sometimes all digits, so those cells are selected by number. `--stop-on-error` stops at the first cell with an error output. Without it every cell still runs, as before. `exec` now exits with code 1 when the code, or any notebook cell, produced an error output; it used to exit 0, so scripts could not tell. `--check` runs a local pre-flight (`colab_cli/notebook.py`) before anything is sent: notebook validation, `--cells` resolution, and a syntax check of each selected cell, compiled with `PyCF_ALLOW_TOP_LEVEL_AWAIT`. IPython `!cmd`, `%magic` and `x = !cmd` lines become `pass`/`None` with their indentation kept, so line numbers match, and `%%` cell magics are skipped. If any check fails, nothing runs and exec exits 2. `--check-only` does the same checks and prints the plan without resolving a session. A missing file or an unreadable notebook now prints one line instead of a traceback.

2026-10-07: Piped `colab console` no longer cuts off its own output on a fresh runtime, and no longer leaks terminal replies. The EOF handler used to close the websocket a fixed 0.5s (`PIPED_EOF_GRACE_SECONDS`) after sending `exit\n`. On a freshly started runtime the first attach often needs 0.5-1s before the shell answers, so the client closed first and nothing came back (2 of 3 fresh-VM runs, traced with timestamps). The backend closes the websocket itself once bash and tmux exit, after the last output, so the handler now waits for that close (`_closed`, set by `on_close`). It only closes from the client side after `PIPED_EOF_CLOSE_TIMEOUT_SECONDS` (30s), as a fallback for a backend that never closes. With a 5s wait, 3 of 3 fresh-VM runs got their output and the backend closed every time, 0.1-1s after `exit`. Separately, tmux sends terminal queries when it attaches (DA1, DA2, XTVERSION, OSC 10/11). With piped stdin the local terminal's replies can never reach the remote side and were left in the terminal's input for the next command. When stdin is not a TTY, `TerminalQueryFilter` now drops those queries from the output.

2026-10-07: Windows console support for `colab console`. cmd.exe and PowerShell have no `termios`, `/dev/tty`, or `SIGWINCH`, so interactive console used to stay line-buffered (arrow keys never left the local line editor, Ctrl-C killed the CLI, ANSI from the remote PTY rendered as garbage in conhost). `console.py` now enables virtual-terminal processing and UTF-8, switches the input handle to raw mode via `SetConsoleMode`, reads keys with `msvcrt.getwch`, translates scan codes to ANSI, and polls the window size. POSIX raw mode and piped stdin are unchanged. `colab ssh --proxy-mode` reads stdin with a blocking `os.read` (Windows `select()` cannot wait on a pipe) and quotes the ProxyCommand with the MSVCRT rules, because OpenSSH for Windows passes it to `CreateProcess` (not `cmd.exe`, as this entry first said).

2026-05-07: Fixed `colab console` piped-stdin handling. Previously a piped invocation (e.g. `echo 'cmd' | colab console -s s`) sent the command and then hung indefinitely because the previous EOF handler emitted a bare `\x04` (Ctrl-D), which the remote `tmux`-wrapped bash treats as a literal character rather than a session terminator. The new handler sends `exit\n` (which bash actually exits on) and then closes the websocket from the client side after a short grace period (`PIPED_EOF_GRACE_SECONDS = 0.5s`) so any tail output (bash `logout`, tmux `[exited]`) makes it back to the user. TTY mode is unchanged: real-terminal EOF is left to the remote shell. Verified live: `echo 'echo HELLO' | colab console -s s` now exits in ~1.2s instead of hanging.

2026-05-07: Fixed `print_kitty` (used by `colab exec --output-image` and any image-producing exec) to no-op when `sys.stdout.isatty()` is false. The Kitty Graphics Protocol escape sequence is meaningless when stdout is a file or pipe and was visually corrupting captured output (a multi-KB base64 PNG blob would land in log files, grep targets, or showboat captures). Image bytes are still saved to disk via `handle_image`'s file-write path; only the inline-render attempt is suppressed.

2026-06-04: Bumped the default `--timeout` for `colab exec` from 10s to 30s (and the matching `colab run` default) so brief silent tasks are less likely to hit a premature `TimeoutError`. Explicit `--timeout` overrides are unaffected.
---

# Design: Execution and Interactive Interaction (`repl`, `exec`, `console`)

## Overview
Execution involves sending Python code (or shell commands) to the Jupyter kernel running on the Colab VM and processing the stream of output messages.

## Approach

### 1. REPL (`colab repl`)
- **Transport**: WebSockets (using `websockets` library if allowed, or a custom `http.client` based long-polling implementation if we're strictly stdlib).
- **Communication**: Jupyter Kernel Messaging Protocol.
    - `execute_request`: Send code string.
    - `execute_reply`: Get status.
    - `iopub.stream`: Capture `stdout` and `stderr`.
- **Interactive Mode**: Standard Python `cmd.Cmd` or `code.InteractiveConsole` for local input/output.
- **Piping Support**: Detect `sys.stdin.isatty()`. If not a TTY, read all input and send as a single execution request.

### 2. Execution (`colab exec`)
- **File Handling**:
    - If file path is local: Read content, send as code.
    - If file path is remote: Execute `!python <path>`.
- **Multi-Modal Output**: Handle `display_data` messages (e.g., `image/png`, `text/html`). For the CLI, we'll save images to temporary files and print their paths, or if the terminal supports it (e.g., iTerm2), inline them.
- **Timeout Configuration**: Exposes a `--timeout` flag (default 30s) to allow long-running silent tasks (like model compilation or data downloading) to execute without being prematurely killed.

### 3. Console (`colab console`)
- **Implementation**: Connects directly to the backend terminal endpoint (`/colab/tty`) via WebSockets using `websocket-client`.
- **Interactive**: Bypasses the Jupyter kernel entirely to provide a raw, PTY-backed bash session on the Colab VM.
- **Terminal Management**: On Linux and macOS, configures `sys.stdin` to raw mode using `termios` and `tty`, passing single characters to the socket and writing raw ANSI escape sequences directly to `sys.stdout.buffer`. Hooks into `SIGWINCH` to communicate local terminal dimensions (`cols`/`rows`) to the remote bash environment so output rendering works perfectly during resizing. On Windows (cmd.exe and PowerShell), `termios` does not exist: the CLI enables `ENABLE_VIRTUAL_TERMINAL_PROCESSING` and UTF-8 on the console, sets raw input mode with `SetConsoleMode` (so the local line editor and Ctrl-C handler get out of the way), reads keys with `msvcrt.getwch`, maps scan codes to ANSI sequences, and polls `os.get_terminal_size` because there is no `SIGWINCH`.
- **Piped stdin**: Detected via `sys.stdin.isatty()`. When piped, the input characters are forwarded one at a time to the remote pty, and on EOF the client sends `exit\n` and waits for the backend to close the websocket. The backend does that once bash and tmux exit, after the last output, so the user's shell goodbye text drains back. The client only closes the websocket itself if that has not happened after `PIPED_EOF_CLOSE_TIMEOUT_SECONDS` (30s), so a piped command running longer than that is cut off. Terminal queries from tmux (DA1/DA2, XTVERSION, OSC colour queries) are dropped from the output in this mode, because the terminal's replies could not reach the remote side. The remote `/colab/tty` endpoint wraps bash in tmux, which intercepts a bare `\x04` as a literal character — that is why we send `exit\n` rather than Ctrl-D.

## Implementation Details
- **Kernel Management**: `ColabRuntime` (from `colab-agent`) already handles message signing and message types.
- **Output Streaming**: Continuous polling or asynchronous message handling to provide real-time output.
- **Piping Example**: `cat script.py | colab exec -s my-session`.

## Testing Strategy
TDD is mandatory for all execution features.

### 1. Mock Kernel Client
- **Test Case**: Verify `ColabRuntime` correctly sends an `execute_request` message over the websocket.
- **Test Case**: Verify `iopub.stream` messages are correctly handled and printed to `stdout` in real-time.
- **Test Case**: Verify `display_data` (specifically `image/png`) triggers the correct local handling (saving or display).

### 2. TTY and Piping
- **Test Case**: Mock `sys.stdin.isatty()` to verify `colab repl` correctly switches between interactive mode and one-shot piped execution.
- **Test Case**: Verify large piped inputs are handled without buffer overflow or truncation.
- **Test Case**: `colab console` with piped stdin sends `exit\n` and calls `ws.close()` on EOF (regression: previously sent `\x04` only and hung).
- **Test Case**: `colab console` in TTY mode does not synthesize an exit on EOF (the user owns the session lifecycle).
- **Test Case**: With `termios` unavailable and `os.name == "nt"`, `connect_console` enters the Windows raw-console context manager and does not call `tty.setraw`.
- **Test Case**: Windows key translation maps scan codes to ANSI and the stdin thread forwards those characters one at a time.
- **Test Case**: `print_kitty` is a no-op when `sys.stdout.isatty()` is false (regression: previously emitted ANSI/base64 into pipes and files).
