---
log:
2026-11-12: `colab exec --json` / `--json-stream` (and `COLAB_CLI_JSON=1`): one JSON record per executed block and a summary on stdout, human output on stderr. Records keep the upstream #237 fields and add `error.location` (the failing line in the local file or notebook cell, past the `--env` prelude), `diagnosis` with `next_actions`, `duration_s` and `session`. Under `--json` a lost session exits 3 and a timeout 4. See docs/08_agent_feedback.md.
2026-11-11: Merged upstream v0.8.0. `colab console` is removed (upstream #205); use `colab ssh -s NAME`, `colab ssh -s NAME -- CMD`, or `!`-prefixed `colab exec`. `--timeout` now uses upstream's streaming idle limit (#185) instead of the hybrid's whole-run watchdog, but keeps the hybrid's no-limit default and still interrupts the kernel on a timeout.
2026-11-03: `--timeout` now defaults to no limit and, when set, interrupts the kernel (the jupyter_kernel_client deadline it replaced busy-looped and could fail finished code). Scripts are read like Python source (coding cookie/BOM, else UTF-8); redirected stdio and colab.log use UTF-8. Live-verified with UTF-8 and cp932 Japanese scripts, piped Japanese I/O, 45 s code without a limit and a 10 s interrupt.
2026-11-03: Added `--secret NAME[=SOURCE]` to `colab exec` (and `colab run`): the value is read locally (env, env:VAR, prompt, hf-cli, gh-cli), never taken from the command line, and written as `***` in the history log. See docs/07_secrets_tunnels_integrations.md.
2026-10-07: `colab exec` notebook control and exit codes. `--cells SPEC` runs only the selected code cells, in the given order. SPEC accepts code-cell numbers (counted from 1, markdown cells not counted), ranges, `#@title` values and cell ids. Entries that look like numbers or ranges are always read as such: nbformat's random cell ids are sometimes all digits, so those cells are selected by number. `--stop-on-error` stops at the first cell with an error output. Without it every cell still runs, as before. `exec` now exits with code 1 when the code, or any notebook cell, produced an error output; it used to exit 0, so scripts could not tell. `--check` runs a local pre-flight (`colab_cli/notebook.py`) before anything is sent: notebook validation, `--cells` resolution, and a syntax check of each selected cell, compiled with `PyCF_ALLOW_TOP_LEVEL_AWAIT`. IPython `!cmd`, `%magic` and `x = !cmd` lines become `pass`/`None` with their indentation kept, so line numbers match, and `%%` cell magics are skipped. If any check fails, nothing runs and exec exits 2. `--check-only` does the same checks and prints the plan without resolving a session. A missing file or an unreadable notebook now prints one line instead of a traceback.

2026-10-07: Piped `colab console` no longer cuts off its own output on a fresh runtime, and no longer leaks terminal replies. The EOF handler used to close the websocket a fixed 0.5s (`PIPED_EOF_GRACE_SECONDS`) after sending `exit\n`. On a freshly started runtime the first attach often needs 0.5-1s before the shell answers, so the client closed first and nothing came back (2 of 3 fresh-VM runs, traced with timestamps). The backend closes the websocket itself once bash and tmux exit, after the last output, so the handler now waits for that close (`_closed`, set by `on_close`). It only closes from the client side after `PIPED_EOF_CLOSE_TIMEOUT_SECONDS` (30s), as a fallback for a backend that never closes. With a 5s wait, 3 of 3 fresh-VM runs got their output and the backend closed every time, 0.1-1s after `exit`. Separately, tmux sends terminal queries when it attaches (DA1, DA2, XTVERSION, OSC 10/11). With piped stdin the local terminal's replies can never reach the remote side and were left in the terminal's input for the next command. When stdin is not a TTY, `TerminalQueryFilter` now drops those queries from the output.

2026-10-07: Windows console support for `colab console`. cmd.exe and PowerShell have no `termios`, `/dev/tty`, or `SIGWINCH`, so interactive console used to stay line-buffered (arrow keys never left the local line editor, Ctrl-C killed the CLI, ANSI from the remote PTY rendered as garbage in conhost). `console.py` now enables virtual-terminal processing and UTF-8, switches the input handle to raw mode via `SetConsoleMode`, reads keys with `msvcrt.getwch`, translates scan codes to ANSI, and polls the window size. POSIX raw mode and piped stdin are unchanged. `colab ssh --proxy-mode` reads stdin with a blocking `os.read` (Windows `select()` cannot wait on a pipe) and quotes the ProxyCommand with the MSVCRT rules, because OpenSSH for Windows passes it to `CreateProcess` (not `cmd.exe`, as this entry first said).

2026-05-07: Fixed `colab console` piped-stdin handling. Previously a piped invocation (e.g. `echo 'cmd' | colab console -s s`) sent the command and then hung indefinitely because the previous EOF handler emitted a bare `\x04` (Ctrl-D), which the remote `tmux`-wrapped bash treats as a literal character rather than a session terminator. The new handler sends `exit\n` (which bash actually exits on) and then closes the websocket from the client side after a short grace period (`PIPED_EOF_GRACE_SECONDS = 0.5s`) so any tail output (bash `logout`, tmux `[exited]`) makes it back to the user. TTY mode is unchanged: real-terminal EOF is left to the remote shell. Verified live: `echo 'echo HELLO' | colab console -s s` now exits in ~1.2s instead of hanging.

2026-05-07: Fixed `print_kitty` (used by `colab exec --output-image` and any image-producing exec) to no-op when `sys.stdout.isatty()` is false. The Kitty Graphics Protocol escape sequence is meaningless when stdout is a file or pipe and was visually corrupting captured output (a multi-KB base64 PNG blob would land in log files, grep targets, or showboat captures). Image bytes are still saved to disk via `handle_image`'s file-write path; only the inline-render attempt is suppressed.

2026-06-04: Bumped the default `--timeout` for `colab exec` from 10s to 30s (and the matching `colab run` default) so brief silent tasks are less likely to hit a premature `TimeoutError`. Explicit `--timeout` overrides are unaffected.

2026-10-24: `--timeout` for `colab exec` (and `colab run`) is now an idle limit: execution fails only after that many seconds without output, so code that keeps printing is never cut off, and `--timeout 0` means no limit. On 0.8.0/0.9.0 the old deadline was never enforced, so code that is silent for longer than `--timeout` now fails where it used to complete. `colab repl` and `colab install`, which pass no timeout, wait until execution finishes, or until the kernel dies or restarts. None of them busy-loop while waiting. `colab auth` and `colab drivemount` are unchanged. See #183.

2026-10-29: Removed the `colab console` subcommand and its `/colab/tty` transport (`console.py`, its tests, and `integration/repro_piped_console/`). Use `colab ssh -s NAME` for an interactive shell and `colab ssh -s NAME -- CMD` for one-shot commands, or pipe `!`-prefixed shell commands to `colab exec` where ssh can't work.
---

# Design: Execution and Interactive Interaction (`repl`, `exec`)

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
- **Timeout**: an idle limit, no limit by default (`0` is also no limit). `--timeout N` fails after N seconds without a message from the kernel, so code that keeps printing is never cut off. On a timeout the hybrid also interrupts the kernel, so the code stops instead of running on in the background, and exits 1 with one line (`Timeout waiting for output: no message from the kernel for Ns. ... The kernel was interrupted.`); the kernel stays usable. `ColabRuntime._execute_streaming` (from upstream #185) waits on the websocket client's queues in 1 s slices instead of `jupyter_kernel_client`'s `execute_interactive`, whose whole-run deadline busy-looped once it passed. Unlike upstream, the CLI default is no limit rather than 30 s, and the kernel is interrupted.
- **Encoding**: local scripts are read with `tokenize.open`, as Python reads source: a PEP 263 coding cookie or a BOM is honored, UTF-8 otherwise. A plain `open()` used the Windows ANSI code page (cp1252, cp1250, cp932, ...) and failed on non-ASCII text. An undecodable script exits 2 with a hint. Redirected stdin/stdout/stderr and `colab.log` use UTF-8 (`errors="replace"` for the streams), so Japanese or other output into a pipe or file no longer raises `UnicodeEncodeError`; a real console is unchanged.
- **Environment and Secrets**: `--env KEY=VALUE` (repeatable) sets plain variables in the kernel's `os.environ` before the code runs. For tokens use `--secret NAME[=SOURCE]` (repeatable), which never takes the value on the command line: SOURCE is `env` (the local `NAME` variable, default), `env:VAR`, `prompt` (hidden, needs an interactive console), `hf-cli` or `gh-cli`. Both are prepended to every executed block as `os.environ[...] = ...`; secret values are registered with the history logger and written as `***`. Secrets are resolved after `--check`/`--check-only` and before the kernel is contacted, so a missing value exits 2 without running anything. To set a secret once for the session, use `colab secret set` (see [07_secrets_tunnels_integrations.md](07_secrets_tunnels_integrations.md)).

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
- **Test Case**: `print_kitty` is a no-op when `sys.stdout.isatty()` is false (regression: previously emitted ANSI/base64 into pipes and files).
