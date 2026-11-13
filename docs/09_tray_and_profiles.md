---
log:
2026-11-13: Open port forwards are detected (`forwards.py`): the ssh processes that `colab forward` / `colab ssh -L` start (any tool, any CLI version) are found in the local process list, matched by session name and sessions file, and their local ports checked with a TCP connect. A runtime behind a forward is a service with an Open button. Live: the forward of a web UI (started by an older global `colab`) showed as a T4 service with `forward 8188`. Also fixed after the first try on a real desktop: sessions.json is read again on every refresh (a runtime created after the tray started was shown as an orphan), orphan cards have no actions, dialogs open on top of the panel.
2026-11-13: Initial design and implementation: progress over the whole run (`overall_percent`, epochs, notebook cells), notebooks as background jobs, `colab exec --var`, run profiles (`profiles.toml`, `colab profile`), the tray snapshot (`tray_model.py`, `colab tray --snapshot`) and the Qt tray panel (`colab tray`, the `[tray]` extra). Live: `integration/repro_tray_profiles/test.sh` (job profile with a snapshot while it runs and after, a notebook job with a `!` cell, a kernel profile that reuses the runtime and sets vars) and a kernel profile that forwards a web server (ready check through `colab forward`). The panel is checked offscreen in tests; on a real desktop it was not yet checked by a person.
---

# Design: Tray Panel, Progress and Run Profiles (`colab tray`, `colab profile`)

## Motivation

Following a run took commands: `colab sessions`, `colab job status`,
`colab usage`. The goal is a compact panel, opened from the system tray on
Windows, macOS and Linux, that shows at a glance what runs, how far it is and
how many compute units (CU) are left, and starts saved run setups
("profiles") with one click. The visual design is a dark panel with one card
per runtime (progress bar, ETA, main metric; Log, SSH and Stop buttons), a
compute-unit card and a "Start profile" split button.

## Principles

- **The tray is its own, optional, long-running process** (`colab tray`). It
  is the one exception to the fire-and-forget rule (AGENTS.md item 7); every
  other command keeps it.
- **The tray changes nothing by itself.** Every action runs one of the CLI's
  subcommands as a child process with the same global options
  (`common.colab_argv()`: the standalone binary runs itself, a Python install
  runs `python -m colab_cli`): `colab job stop`, `colab stop`,
  `colab profile run`, and `colab job tail` / `colab log` / `colab ssh` in a
  new terminal window.
- **The tray never keeps a runtime alive.** It sends no keep-alive and does
  not use the kernel. It reads a runtime's job files (Contents API) on every
  poll only while a job runs there; a runtime without a running job is looked
  at again every 5 minutes (to notice a job started from a terminal); an
  orphan (no local session) never. The account's CU info is read every 5
  minutes. A foreground `colab exec` is seen from the local state only.
- **No secrets** on the panel, in notifications or in profiles: a profile
  names `colab secret` entries.

## 1. Progress over the whole run

What `colab job status` already had: the tqdm bar of the current line
(percent, n/total, ETA), `colab_feedback.report()` metrics with their trend,
a stall flag, the last output and a diagnosis. Added:

- `report(epoch=, epochs=)` (epoch counted from 1) or `report(step=, total=)`
  give `overall_percent`: `(epoch - 1 + bar/100) / epochs`, or
  `step / total`. These counters are left out of the metric trend.
- **Notebooks as background jobs**: `colab job start -f nb.ipynb` and
  `colab run --detach nb.ipynb`. The CLI sends the non-empty code cells
  (`cells.json`, labels as in `colab exec`: `cell 3 (Setup)`) and a driver
  (`job_vm.NOTEBOOK_SOURCE`) that runs them in order in one IPython shell
  (`InteractiveShell`, plain tracebacks), so `!cmd`, `%magic` and `%%cell`
  magics work; the first failing cell ends the job with exit code 1. Each
  cell prints `##colab-cell I/N LABEL` first; the runner turns it into
  `status.cell` and resets the bar. Without IPython the cells run as plain
  Python. `colab run` without `--detach` refuses a notebook (exit 2), before
  a runtime is created.
- The job record (`colab job status --json`) gains `cell`, `epoch`
  (`{current, total}`), `overall_percent` (100 once succeeded) and
  `last_output_age_s` (also after the job ended).
- A foreground `colab exec` records the cell it runs in
  `SessionState.activity` (`file`, `cell`, `total`, `label`, `started_at`);
  `colab status` prints it and the tray reads it without touching the VM.

## 2. Run profiles

`profiles.toml`, next to `sessions.json` (it follows `--config`), read with
`tomllib`, written owner-only. `colab profile add` and `rm` rewrite the file
(comments are lost); `colab profile path` prints it for hand editing.

```toml
[train-t4]                       # mode = "job" (default)
script = "C:/work/train.py"      # .py or .ipynb; relative to profiles.toml
args = ["--epochs", "3"]
gpu = "T4"                       # or tpu = "V5E1"; neither: CPU
high_mem = false
budget = 2.0                     # CU, enforced on the runtime
secrets = ["HF_TOKEN"]
env = { WANDB_MODE = "offline" }
stop_when_done = true            # default: the tray stops the runtime after the job
description = "Fine-tune on a T4"

[comfyui]                        # a server in the kernel, forwarded
script = "~/work/comfyui_server.py"
mode = "kernel"
session = "comfyui"              # fixed name; reused while it is alive
gpu = "T4"
vars = { SERVER_CONFIG = { access = "forward" } }
forward = [8188]                 # PORT, LOCAL:REMOTE or LOCAL:HOST:REMOTE
ssh_key = "~/.ssh/colab_key"
ready = "http://127.0.0.1:8188/system_stats"
open = "http://127.0.0.1:8188"
stop_when_done = false
```

Validation (pydantic `Profile`, unknown keys rejected by name): GPU/TPU from
the same tables as `colab new`; not both; `budget > 0` and only in job mode;
`vars` only in kernel mode (a job is its own process; use `env`); `args` only
in job mode; `vars` names must be Python names; `forward` entries must look
like ports. `ready` defaults to `open`.

`colab profile run NAME`:

- **job**: on its live `session`, `colab job start -f SCRIPT`; otherwise
  `colab run --detach -s NAME-MMDD-HHMMSS ...` (a new runtime). The session
  is marked with `profile` and `stop_when_done` (new optional
  `SessionState` fields); the tray stops it once the job ends. Without the
  tray the runtime keeps running, which the command says.
  `--foreground` runs `colab run` and waits (the runtime stops at the end).
- **kernel**: with `forward`, the SSH key is checked first (as `colab ssh`
  does). Then `colab new` (or reuse), `colab exec -f SCRIPT` with
  `--var NAME=JSON` for each `vars` entry, then `colab forward` in the
  foreground; once `ready` answers (any status below 500), `open` is opened
  in the browser (`--no-open` skips it). Ctrl+C closes the forward. With
  `stop_when_done` the runtime is stopped when the run ends (also after a
  failure).

`colab exec --var NAME=VALUE` (VALUE is JSON, else a string) sets Python
globals once, before the first cell; useful for any notebook or script that
reads settings from globals (e.g. `#@param` cells).

## 3. The snapshot (`tray_model.py`)

`collect_snapshot(state, prev)` returns `{schema, type: "tray_snapshot",
taken_at, last_ok_at, ok, error, account, sessions[]}`; each session has
name, endpoint, hardware, `local`, `profile`, `stop_when_done`, `running`,
`activity`, `last_execution_at`, the newest `job` record and
`jobs_checked_at`. A failed refresh keeps the previous data (`ok` false,
`error` set). Pure functions on top of it:

- `icon_state(snap, now, interval)`: level, colour and the same in words.
  Worst first: **stale** (no successful refresh for two intervals), **error**
  (refresh failed, a failed job), **warning** (no output from a job, less than
  an hour of CU at the current rate, a runtime idle for more than 75 minutes,
  which Colab reclaims at about 90), running, idle, none. `reasons` carries
  the codes for translation.
- `card_model(entry)`: state (running, stalled, done, failed, stopped, idle,
  service), progress (`overall_percent`, or the cell of a foreground exec);
  without progress the card shows the age of the last output; main metric
  (`loss` if reported) with its trend; epoch, cell, ETA, diagnosis.
- `cu_model(account)`: balance, rate and hours left; always marked as an
  estimate (balance divided by the current rate).
- `events(prev, cur)`: job finished, failed (with its diagnosis code),
  stopped, stopped by the budget; a runtime gone; the balance dropping under
  an hour.
- `sessions_to_stop(snap, already)`: profile runtimes with `stop_when_done`
  whose job ended.

`colab tray --snapshot [--json]` prints the snapshot (with `icon`, `cards`
and `cu`) and exits; it needs no GUI, so scripts, agents and the live test
use it.

### Port forwards

`forwards.scan_forwards()` finds the forwards open on this computer without
asking the runtime: `colab forward` and `colab ssh -L` run
`ssh -o ProxyCommand="... ssh --proxy-mode -s NAME ..." -L LOCAL:HOST:REMOTE`,
so the ssh processes are listed (Windows: `Get-CimInstance Win32_Process`,
Linux: `/proc/*/cmdline`, macOS: `ps`), parsed for the session name, the
`--config` of the ProxyCommand and the `-L` values, and each local port is
checked with a TCP connect to 127.0.0.1. Only forwards of the same sessions
file count. Each snapshot entry gets `forwards` (`local_port`, `remote`,
`listening`); the card becomes a **service** with "forward: 8188" and an
**Open** button (`http://127.0.0.1:PORT`, or the profile's `open` URL), and
a forwarded runtime is not warned about as idle (the forward keeps it alive).
Whichever tool started the forward does not matter, nor its CLI version.

## 4. The panel (`tray/`, the `[tray]` extra)

`uv tool install "google-colab-cli[tray] @ git+https://github.com/arlinamid/google-colab-cli"`
adds PySide6-Essentials. Qt was chosen over pystray (a native menu cannot
draw cards and progress bars) plus a separate window: one event loop for the
icon, the panel and notifications (no main-thread clash on macOS), a tray
icon on Linux without PyGObject (StatusNotifier), and native SVG.

- `colab tray [--interval 30] [--detach]`: refuses without a desktop (no
  `DISPLAY`/`WAYLAND_DISPLAY`, or WSL: exit 2, pointing to `--snapshot`) or
  without the extra (exit 2 with the install command); one instance per
  config directory (`tray.lock`); `--detach` restarts itself without a
  console window.
- `app.py`: a `QTimer` starts `collect_snapshot` on a `QThreadPool` worker
  (the only thread that uses the CLI state); the result arrives as a signal.
  The icon (`icon.py`) is the Colab logo with a coloured dot; the tooltip says
  the state in words (`actions.status_text`), errors and stale data included.
- `panel.py`, after the design: header (logo, connection state, settings);
  "Active sessions" with a count; one card per runtime in a scroll area that
  shows three at most (more scroll); the CU card with "Estimated time left";
  the "Start profile" split button (main button: the last profile used;
  arrow: all profiles); Refresh, Settings; "Updated: ..." (yellow when stale)
  and Quit. Line icons are drawn in `glyphs.py`; the metric arrow is green
  when it moves the good way (a loss going down). The panel hides when it
  loses focus, except while one of its dialogs is open.
- **Stop** asks first, and each choice says what stops: only the job (the
  runtime keeps running and using CU) or the whole runtime (unsaved files
  are lost; a forward or service the tray started is closed too). Without a
  running job only the runtime can be stopped.
- **Start profile** asks first, naming the mode, hardware, the account's
  current rate and the session it reuses. The tray runs
  `colab profile run NAME` as a child (output in `tray-logs/NAME.log`); a
  kernel profile that keeps running shows as a "service" card with an
  **Open** button; a non-zero exit is notified with the log path.
- Notifications follow `events()`; `stop_when_done` runtimes are stopped
  once (`colab stop`), with a notification.
- Settings (`settings.json`): refresh interval, notifications, language
  (system, Hungarian, English). The panel's texts are in `i18n.py`; the CLI
  stays English.
- The Colab logo (`tray/colab.svg`) comes from homarr-labs/dashboard-icons
  (Apache-2.0); the logo is a trademark of Google.

## Testing

- `tests/test_job_vm.py`: cell markers, the notebook driver with IPython and
  as plain Python. `tests/test_jobs.py`: `overall_percent`, `epoch`, `cell`,
  `last_output_age_s`, notebook cells and files. `tests/test_job_cmd.py`,
  `tests/test_run_json.py`: notebook jobs, `run` refusing a notebook without
  `--detach`. `tests/test_exec_notebook_options.py`: `activity`, `--var`.
- `tests/test_profiles.py`, `tests/test_profile_cmd.py`: the file, validation,
  writing, and `profile run` (the subcommands it calls, reuse, forward,
  readiness, stop_when_done, key check).
- `tests/test_tray_model.py`: the snapshot (no proxy access for idle runtimes
  between rechecks), icon levels, cards, CU, events, stop targets.
  `tests/test_tray_actions.py`: texts in both languages, stop choices,
  commands, terminals. `tests/test_tray_cmd.py`: snapshot, desktop and extra
  checks, the lock, `--detach`. `tests/test_tray_panel.py`: the panel and
  controller on Qt's offscreen platform (skipped where Qt cannot load).
- Live: `uv run bash integration/repro_tray_profiles/test.sh`.

## Not done

- Starting the tray at login.
- The panel on a real Linux desktop and on macOS was not tried (unit tests
  only); on Windows it was rendered offscreen.
