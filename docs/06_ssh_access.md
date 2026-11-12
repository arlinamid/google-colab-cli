---
log:
2026-11-12: Linux: ssh is started with `preexec_fn=_ssh_preexec`, which sets `PR_SET_PDEATHSIG` (SIGTERM), so a SIGKILL of colab also ends ssh and, through ssh, its ProxyCommand; the standalone binary's CLI process ties itself to its PyInstaller launcher the same way (`_tie_to_launcher_linux`, only when the parent runs the same exe). macOS has no equivalent; a closed terminal (SIGHUP) still ends everything there. Git Bash: the remote `-- CMD` arguments are passed through `utils.undo_msys_args`, so `colab ssh -- ls /content` works without `MSYS_NO_PATHCONV`. Live on the Windows binary: ssh path and launcher kill; on the Linux binary: launcher SIGKILL.
2026-11-12: `colab ssh` (interactive, `-- CMD`, `-N`) and `colab forward` check that a key file exists before they create a runtime or start ssh (`_require_ssh_key`), and exit 2. Before, the ProxyCommand child found the key missing, so the user got ssh's exit 255 after an interactive `colab ssh` had already assigned a runtime. Without a default key, the message names other ed25519/ECDSA keys in `~/.ssh` and how to pass one with `-i`. Added `tests/test_ssh_key_precheck.py`.
2026-11-12: Standalone binary on Windows: the PyInstaller onefile launcher is the parent of the process that runs the CLI, from the same exe. That process now hands the only kill-on-close job handle to the launcher (`_hand_job_to_launcher`, `DuplicateHandle`), so a forced kill of the launcher (closed tab, `taskkill /F`) also ends the CLI process, ssh.exe and the proxy. Before, only the launcher died and they kept running. Verified with the venv python.exe launcher: after `taskkill /F` on it, neither its child nor the child's process was left.
2026-11-03: No leftovers: on Windows `colab ssh`/`colab forward` put their process in a KILL_ON_JOB_CLOSE job object before starting ssh, so a killed colab (closed terminal tab) also ends ssh.exe and the proxy (verified live: 0 ssh.exe after closing the tab). The proxy's WebSocket connect is bounded to 30 s, then the socket timeout is lifted.
2026-11-03: On Windows `colab ssh`/`colab forward` run Windows OpenSSH (%SystemRoot%\System32\OpenSSH) even when Git Bash puts MSYS ssh first on PATH (MSYS ssh ran the ProxyCommand through /bin/bash and lost the backslashes). A passphrase prompt without a terminal exits 2 instead of hanging: the parent tells the ProxyCommand child via COLAB_CLI_SSH_CAN_PROMPT, and ssh gets BatchMode=yes. Live: a ComfyUI forward on a T4 runtime from Git Bash returned HTTP 200.
2026-11-03: Added port forwarding: `colab ssh -L SPEC` (repeatable; shell, `-- CMD` and `-N/--no-shell`) and `colab forward -s S PORT...`, with `ExitOnForwardFailure=yes`. While a forward is open the CLI pings the TFE keep-alive endpoint every 60s from its own process (no daemon); `--no-keep-alive` turns it off. See docs/07_secrets_tunnels_integrations.md.
2026-11-09: `--identity/-i` now accepts either a `.pub` public key or a private key path: `_resolve_pubkey` reads the `.pub` file directly when present before falling back to `_pubkey_from_identity` (`ssh-keygen -y -f`), and `_ssh_base_args` strips `.pub` for `ssh -i` when the sibling private key exists on disk.
2026-10-29: Removed the `colab console` subcommand, so `colab ssh` is the interactive shell on the runtime. Dropped console behavior from the Windows validation caveat.
2026-10-29: Added `colab ssh [-s NAME] -- CMD`, which runs one command on an existing session and exits with its status (a PTY only when stdin and stdout are terminals; neither it nor its ProxyCommand child creates a runtime). Its handshake carries `ssh-mode-cmd`, and the update banner is skipped for `ssh` when stdout is not a terminal. Added `tests/test_ssh_cmd.py`.
2026-10-29: HTTP 429 now shows the server's SSH session limit ("N of N SSH sessions in use") when the body carries one.
2026-10-29: The ProxyCommand that `colab ssh` starts now keeps a non-default global `--auth` and `--config` (as an absolute path), so `colab --config ALT ssh -s S` bridges S from ALT instead of the default sessions file.
2026-10-29: Every `/colab/ssh` handshake now sends `X-Colab-Client-Agent: colab-cli` and the two `colab-client-agent` query values `colab-cli` and `ssh-mode-<mode>`. The mode is `interactive` for plain `colab ssh`, `cmd` for `colab ssh -- CMD` (both pass it to their ProxyCommand re-invocation through a hidden `--colab-internal-mode` option), and `proxy` otherwise.
2026-10-20: Fixed Windows SSH ProxyCommand quoting, local null-device selection, and cleanup signal registration. Added native Windows regression and live integration tests; verified the SSH suite on Windows, Linux, and macOS, plus live Windows remote execution and runtime cleanup.
2026-09-25: Cleaned up keep-alive daemon references in session creation and teardown documentation.
2026-08-09: Added `--high-mem` passthrough when `colab ssh` auto-creates a runtime (forwards to `colab new --high-mem`).
2026-07-17: Initial design and implementation of `colab ssh` — client side of SSH-over-WebSocket runtime access. Adds three modes (interactive shell, `-s SESSION`, and `--proxy-mode` OpenSSH ProxyCommand bridge), `--identity/-i` key selection, and per-HTTP-status handshake error messages. Server side is out of scope for this repo; the subcommand is a no-op against runtimes that do not expose the `/colab/ssh` endpoint (surfaces an actionable HTTP 404 message).
2026-07-22: Bare `colab ssh` now auto-creates a runtime (via `colab new`) when you have no active session, with `--gpu/--tpu` passthrough and `--rm` to stop an auto-created runtime on exit. Fixed two client bugs: the dead 403 branch (feature-off returns 404, not 403) and the RSA guidance (all `ssh-rsa` keys are server-rejected, so `id_rsa` is no longer auto-scanned and the 400 message no longer advertises `rsa-sha2`). Added `tests/test_ssh_wire_contract.py` (real loopback-server wire assertions) and `tests/test_ssh_autocreate.py`.
2026-07-22: Interactive `colab ssh` now starts in `/content` (Colab's working dir) instead of `/root`, via a forced PTY (`-t`) plus a remote `cd /content 2>/dev/null; exec $SHELL -l`. A missing `/content` falls back to the login home. Added `tests/test_ssh_workdir.py`.
2026-07-22: `--proxy-mode` now honors every `colab ssh` flag: with `-s NAME` it creates the session if missing (creation output routed to stderr so stdout stays the clean ssh byte stream), `--gpu/--tpu` set the accelerator, and `--rm` stops the bridged session on disconnect — so `~/.ssh/config` hosts work on first connect and can be made ephemeral. Removed the `--drive` subfeature entirely (code + tests).
2026-07-22: Fixed `--proxy-mode --rm` not tearing down on disconnect. OpenSSH sends the ProxyCommand SIGHUP (verified empirically) when the session ends — not just stdin EOF — and Python's default SIGHUP action terminated the process before the teardown `finally` ran, leaking the runtime + keep-alive daemon. Now `--rm` installs SIGHUP/SIGTERM/SIGINT handlers that run the stop (idempotent with the `finally`).
2026-07-23: Applied go/pystyle readability to the ssh code (80-col reflow, Args/Returns/Raises docstrings) and upgraded the integration test from a `--help` smoke into a real end-to-end: it drives a live remote command over `colab ssh --proxy-mode` used as an OpenSSH ProxyCommand (handshake + pubkey-header auth + bridge + remote exec), asserts the RSA-key rejection, and verifies no orphan VM — plus an always-on offline check (help flags + unknown-session exit 2). The live part now auto-runs when auth is present instead of being `RUN_LIVE`-gated.
2026-07-24: Refactored `ssh()` into intent-named helpers (`_select_proxy_session`, `_select_interactive_session`, `_run_proxy_bridge`, `_run_interactive_shell`, `_install_rm_signal_handlers`, `_warn_accelerator_ignored`) — behavior-preserving — and unified the two `--gpu/--tpu ignored` messages into one. Added `tests/test_ssh_lifecycle.py` pinning lifecycle guarantees: `--rm` teardown survives an exception (try/finally), ssh/bridge exit-code propagation, `--proxy-mode` stdout cleanliness (create/`--rm` chatter stays on stderr), auto-create failure aborts before connect, `--gpu`+`--tpu` both forwarded to `colab new`, `--rm` idempotency across the signal + finally paths, and reused-session `--rm` teardown. Trimmed prose in this doc + the integration README.
---

# Design: `colab ssh` — SSH-over-WebSocket Runtime Access

## Motivation
Users want a real shell on their Colab runtime and, more importantly, IDE
remote-development (VS Code Remote-SSH, JetBrains Gateway, plain `ssh`). `colab ssh` is
the client that allows sshing into Colab, reusing the CLI's existing session resolution and
runtime-proxy token so no separate credential handling is needed.

## User Surface

```
colab ssh [OPTIONS] [-- CMD [ARGS]...]
```

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `-s`, `--session` | str | auto | Session to connect to. If omitted, uses your only active session, auto-creates one when you have none (not with `-- CMD`), or errors when you have several. |
| `--proxy-mode` | bool | False | Act as an OpenSSH `ProxyCommand`-compatible WebSocket↔stdio bridge (reads stdin, writes stdout) for `~/.ssh/config`. Every other flag still applies. |
| `-i`, `--identity` | str | auto | SSH key (`.pub` public key or private key path) for the public key sent to Colab. Default: first of `~/.ssh/id_ed25519.pub`, `id_ecdsa.pub`. |
| `--gpu` | str | None | GPU accelerator for a runtime this command creates (T4, L4, G4, H100, A100). |
| `--tpu` | str | None | TPU accelerator for a runtime this command creates (v5e1, v6e1). |
| `--high-mem` | bool | False | Request high-RAM when this command auto-creates a runtime (ignored when connecting to an existing session). |
| `--rm` | bool | False | Stop the runtime when the session ends. Interactive: only a runtime `colab ssh` auto-created (a reused session is never removed). `--proxy-mode`: the bridged session, on disconnect. Ignored with `-- CMD`. |
| `-L`, `--forward` | str (repeatable) | None | Forward a local port to the runtime: `PORT`, `LOCAL:REMOTE` or `LOCAL:HOST:REMOTE` (host defaults to `localhost` on the runtime). Works with the shell, `-- CMD` and `-N`. Ignored with `--proxy-mode` (use `LocalForward` in `~/.ssh/config`). |
| `-N`, `--no-shell` | bool | False | With `-L`: only hold the forwards open, no shell; Ctrl+C stops. |
| `--no-keep-alive` | bool | False | With `-L`: do not ping keep-alive (the runtime may idle out). |

### `~/.ssh/config` usage
`--proxy-mode` turns `colab ssh` into a transport any SSH-based tool can drive:

```
Host <alias>
  ProxyCommand <abs-path-to>/colab ssh --proxy-mode -s <name> [--gpu T4] [--rm]
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
```

Because every flag applies in `--proxy-mode`, `-s <name>` creates the session on
first connect, `--gpu/--tpu` size it, and `--rm` makes the host ephemeral. Use an
**absolute** `colab` path: `ssh` runs the `ProxyCommand` in a non-login shell
where a bare `colab` may not be on `PATH`. External SSH tools run their own
remote command, so to also land in `/content` add `RequestTTY yes` and
`RemoteCommand cd /content 2>/dev/null; exec bash -l`.

### Port forwarding
Reach a web UI on the runtime (Gradio, Streamlit, Jupyter, TensorBoard) at
`http://localhost:PORT`, privately (only your machine can connect):

```
colab forward -s NAME 7860              # = colab ssh -s NAME -N -L 7860
colab ssh -s NAME -L 8080:8888          # local 8080 -> runtime 8888, plus a shell
colab ssh -s NAME -L 6006 -- tensorboard --logdir runs   # forward while CMD runs
```

- `-o ExitOnForwardFailure=yes` is added, so a local port that is already in
  use makes ssh exit instead of running without the forward.
- **Keep-alive**: the backend keeps a runtime alive only on kernel activity,
  so forward traffic alone would let it idle out (~90 min). While a forward is
  open, `colab` runs ssh under `Popen` and pings
  `GET /tun/m/<endpoint>/keep-alive/` every 60 s from its own process (no
  daemon, no background thread) until ssh exits. A failed ping warns once; a
  404 (runtime gone) stops pinging. `--no-keep-alive` turns it off.
- `colab forward` and `-N` run until Ctrl+C (exit 130). In scripts, use
  `-L PORT -- CMD`, which ends with CMD.
- For a public URL instead, see `colab share` in
  [07_secrets_tunnels_integrations.md](07_secrets_tunnels_integrations.md).

### Remote command
`colab ssh [-s NAME] -- CMD [ARGS]...` runs CMD in `/content` on an existing
session (never creating one); every word from CMD on goes to CMD, flags too.

- One argument is a shell command line (`-- 'ls / | wc -l'`); several are an
  exact argv, so `*`, `~` and `$VAR` in separate arguments are not expanded.
  Scripts can also pipe in: `echo cmd | colab ssh -s NAME -- bash`.
- `-t` only when stdin and stdout are terminals; otherwise no PTY, so piped
  output is byte-exact. The exit status is CMD's; 255 means an ssh error or a
  remote command killed by a signal (e.g. SIGPIPE from `| head`).
- A runtime accepts a limited number of concurrent SSH connections; past it
  the server answers 429 and `colab ssh` prints its message.
- With a terminal on both ends, Ctrl-C reaches the remote command and ssh
  exits 255. Piped, Ctrl-C stops only `colab ssh` (exit 130) and the remote
  command may keep running: stop it with a pattern that cannot match its own
  shell, e.g. `colab ssh -s NAME -- pkill -f '[s]leep 300'`, or `colab stop`.
- SSH sessions use a minimal environment: Python, pip and `nvidia-smi` work;
  tools such as `gcloud` or `node` may need their directories added to `PATH`.

## Behavior

1. **Session resolution / auto-create**: With `-s NAME`, resolves that session
   (via `state.resolve_session`, the same helper the other commands use). Bare
   `colab ssh` uses your only active session; with **no** session it auto-creates
   one (mirrors `colab new`: assign → persist `SessionState`; never with
   `-- CMD`); with **multiple**
   it errors and asks you to pick one with `-s`.
2. **Connect**: Opens the WebSocket to `wss://<netloc>/colab/ssh?colab-runtime-proxy-token=<token>`
   and sends the resolved public key verbatim in the `X-Colab-Ssh-Pubkey` header
   (no transformation -- the bytes the user controls are exactly what the server
   receives). Only `ssh-ed25519` / `ecdsa-sha2-nistp{256,384,521}` keys are
   accepted. It also sends `X-Colab-Client-Agent: colab-cli` and appends
   `&colab-client-agent=colab-cli&colab-client-agent=ssh-mode-<mode>`: `interactive`
   for plain `colab ssh`, `cmd` for `colab ssh -- CMD`, `proxy` for a
   user-written `ProxyCommand` or an IDE.
3. **Interactive shell**: Spawns the system `ssh` binary with the CLI re-invoked
   as its own `ProxyCommand` (`python -m colab_cli.cli ssh --proxy-mode --colab-internal-mode interactive`), so the
   WebSocket bridge and the interactive shell share one code path. It forces a
   PTY (`-t`) and runs `cd /content 2>/dev/null; exec $SHELL -l` so you land in
   `/content` (Colab's working dir) rather than root's home; a missing `/content`
   falls back to the login home.
   On native Windows, the ProxyCommand uses `subprocess.list2cmdline` for
   CreateProcessW argument parsing; POSIX systems continue to use `shlex.join`.
   The local `UserKnownHostsFile` uses `os.devnull` (`nul` on Windows).
4. **`--proxy-mode` bridge**: Bridges the WebSocket ↔ stdin/stdout for use as an
   OpenSSH `ProxyCommand`. Honors every flag: `-s NAME` creates the session if
   missing (creation output routed to stderr so stdout stays the clean ssh byte
   stream); bare `--proxy-mode` with no `-s` just resolves an existing session.
5. **`--rm` teardown**: Stops the runtime when the session ends. In `--proxy-mode`
   this must survive how OpenSSH ends a `ProxyCommand`: on disconnect it sends
   **SIGHUP** (verified), not just stdin EOF, and Python's default SIGHUP action
    would terminate the process before the teardown `finally` ran — leaking the
    runtime. `--rm` therefore installs
   handlers for the available SIGHUP/SIGTERM/SIGINT signals that run the stop,
   idempotent with the `finally`. `SIGKILL` cannot be intercepted, so a `kill -9`/hard crash can
   still leak; a normal disconnect is SIGHUP and is handled.
   Windows has no SIGHUP, so registration skips it and retains SIGTERM/SIGINT.
   Forced process termination on Windows can also bypass cleanup; this change
   does not guarantee teardown after a killed process.
6. **Error handling**: The WebSocket upgrade maps each common HTTP status to an
   actionable message:

   | Status | Meaning surfaced to the user |
   | --- | --- |
   | 400 | Bad/unsupported/missing pubkey, with remediation (`ssh-keygen -t ed25519`) |
   | 401 | Token invalid/expired — try `colab new` |
   | 403 | Forbidden — token lacks permission for this action (feature-off returns 404, not 403) |
   | 404 | SSH not exposed on this runtime — SSH is baked in at creation, so run `colab new` |
   | 429 | Session limit reached: "N of N SSH sessions in use", and the limit depends on the Colab plan; without a usable limit in the body, another `colab ssh` is connected: disconnect it first |
   | 502 | Runtime `sshd` unreachable — runtime may be unhealthy |
   | other / none | Raw status or a network-check hint |

7. **Key check**: every mode that starts ssh itself (not `--proxy-mode`)
   first checks that a key file exists: `-i PATH` (either half of the pair)
   or a default `~/.ssh/id_ed25519.pub` / `id_ecdsa.pub`. Otherwise it exits 2
   before resolving or creating a session, naming other ed25519/ECDSA keys
   found in `~/.ssh`. Reading the key (and a passphrase prompt) stays in the
   ProxyCommand child.
8. **Remote command**: `-- CMD` resolves an existing session and runs CMD as
   described under Remote command. Its ProxyCommand child (mode `cmd`) only
   resolves the session too, so neither process creates a runtime.
9. **Port forwarding**: each `-L` spec is validated (ports 1-65535, exit 2
   otherwise) and becomes `-L LOCAL:HOST:REMOTE` plus
   `-o ExitOnForwardFailure=yes`. With forwards and keep-alive on, ssh runs
   under `Popen` and `keepalive.wait_with_keep_alive` pings between
   `proc.wait(timeout=60)` calls; the exit code is ssh's (130 after Ctrl+C).
   `-N` without `-L` is an error (exit 2). Tests: `tests/test_ssh_forward.py`,
   `tests/test_keepalive.py`.

## Testing Strategy (TDD)

### Unit tests (`tests/test_ssh.py`)
1. WebSocket URL construction (`wss` for https, `ws` for http; token query param).
2. Pubkey resolution — `--identity` (reads the matching `.pub` file first,
   whether passed with or without `.pub`, before falling back to `ssh-keygen -y
   -f`) and the `~/.ssh` default scan; missing-key and missing-identity exit
   paths.
3. The full status→message map (400/401/403/404/429/502/other/none).
4. Shell quoting for the `ProxyCommand` string.
5. Session resolution (existing vs missing).
6. End-to-end dispatch: interactive vs `--proxy-mode`, including a
   verbatim-pubkey pass-through assertion and the actionable-400 message.

### Wire-contract tests (`tests/test_ssh_wire_contract.py`)
Stands up a loopback WebSocket server and drives the real connect path (no mock)
to assert the request path, the `colab-runtime-proxy-token` query param, the
`X-Colab-Ssh-Pubkey` header, the `X-Colab-Client-Agent` header and the two
`colab-client-agent` query values reach the wire verbatim. Includes mutation tests
that fail if `_SSH_PATH`/`_PUBKEY_HEADER`, the client-agent header or a mode value
drift, real HTTP 400/429 mapping (including a 429 body with a limit) via a genuine
`WebSocketBadStatusException`, and real CLI child processes for plain `colab ssh`
(`interactive`) and a user `ProxyCommand` (`proxy`).

### Auto-create & proxy-mode tests (`tests/test_ssh_autocreate.py`)
Bare `colab ssh` create vs reuse vs ambiguous; `--gpu/--tpu` passthrough; `--rm`
stop-on-exit; and the `--proxy-mode` matrix — create-if-missing with `-s NAME`,
reuse of an existing session, `--gpu` passthrough, `--rm` teardown, and the
SIGHUP cleanup handler being installed only under `--rm`.

### Working-directory tests (`tests/test_ssh_workdir.py`)
Interactive `ssh` forces a PTY (`-t`) and runs a `cd /content` remote command
(host before the command, `2>/dev/null` tolerance for a missing directory).

### Remote-command tests (`tests/test_ssh_cmd.py`)
`-- CMD` forwarding and quoting, the `-t`/`-T` rule, exit status, no
auto-create in the parent or its ProxyCommand child, `ssh-mode-cmd` at
`websocket.WebSocket.connect`, closed stdio, and the update-banner gate.

### Integration test (`integration/repro_ssh/`)
Two parts. An offline smoke that always runs (no VM): ``--help`` advertises the
documented flags, and an unknown session exits 2 with an actionable message. A
live end-to-end that runs when auth is present (allocates a CPU VM): it uses
``colab ssh --proxy-mode`` as an OpenSSH ProxyCommand to run a real remote
command over the WebSocket bridge -- exercising the same connect ->
pubkey-header auth -> handshake -> bridge -> remote-exec path as the interactive
shell, minus the TTY -- asserts the RSA-key rejection, and verifies ``colab
stop`` leaves no orphan VM.

### Windows regression tests (`tests/test_windows_ssh.py`)
Real Windows child processes run the generated ProxyCommand using a Python
interpreter installed under a path with spaces. Assertions check exact argv
preservation for spaces, quotes, shell punctuation, backslashes, Unicode, and
identity paths. A separate test launches the real Windows OpenSSH client with
a local stub ProxyCommand and verifies its argv, without contacting Colab.
Portable tests also check registration without SIGHUP and the local null
device. Existing POSIX shell quoting and teardown tests remain in the suite.

These and the existing SSH tests passed on Windows, Linux, and macOS with
Python 3.12. This covers SSH compatibility only; local file editing and
self-update require separate platform validation.

`uv run python integration/repro_windows_ssh/test.py` provides a native Windows
live test using cached OAuth2 credentials. It creates a CPU runtime, executes a
remote command through the generated ProxyCommand with a key path containing
spaces, exercises `--rm`, and cleans up the recorded endpoint on failure. Exit
77 is a prerequisite skip, not a successful live validation.

The native Windows live test passed on 2026-10-20 with Python 3.12.14 and the
Windows OpenSSH client. This validates remote command execution and cleanup
for this SSH path, not overall native Windows feature parity.
