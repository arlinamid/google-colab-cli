---
log:
2026-11-03: Initial design and implementation: secret specs and history redaction (`colab secret set`, `--secret` on `exec`/`run`), ssh port forwarding with keep-alive (`colab ssh -L`, `colab forward`), `colab hf login/download`, `colab clone`, `colab share` (cloudflared/ngrok) and `colab keepalive`. Verified live on Windows against a CPU runtime.
---

# Design: Secrets, Port Forwarding, Tunnels and Integrations

## Motivation

Common workflows on a runtime need a token (Hugging Face, GitHub, ngrok,
W&B, an API key) or a way to reach a web UI (Gradio, Streamlit, Jupyter,
TensorBoard) served by the runtime. Before this change:

- `--env KEY=VALUE` put a token on the command line (shell history, process
  list), and the injected `os.environ[...] = '...'` code was written verbatim
  to `~/.config/colab-cli/history/<session>.jsonl`, where `colab log` shows it.
- Reaching a port on the runtime needed a third-party tunnel set up by hand.
- Client-side keep-alive was removed upstream (`d8a2b92`): the backend keeps a
  runtime alive only on kernel activity, so ssh or tunnel traffic alone lets it
  idle out (~90 minutes).

## Secrets (`colab_cli/secrets.py`)

A secret is given as `NAME[=SOURCE]`:

| SOURCE | Value comes from |
| --- | --- |
| `env` (default), `env:VAR` | Local environment variable `NAME` / `VAR` |
| `prompt` | Hidden prompt (`getpass`) on the terminal |
| `hf-cli` | `hf auth token`, then the stored token file (`HF_TOKEN_PATH`, `HF_HOME/token`, `~/.cache/huggingface/token`) |
| `gh-cli` | `gh auth token` |

- A literal value (`NAME=hf_abc`) is refused, and the error never echoes it.
- CLI sources run with `stdin=DEVNULL` and a timeout; the executable is found
  with `shutil.which`, so Windows launchers (`gh.cmd`, `hf.exe`) work.
- Every resolved value is registered with `HistoryLogger.add_redaction`, which
  replaces the raw value and its `repr()` body with `***` in every logged
  event (code, outputs, stdin), recursively, before writing.

Commands: `colab secret set -s S SPEC...` (into the session kernel's
`os.environ`), and `--secret SPEC` on `colab exec` / `colab run` (prepended
like `--env`). `--check-only` does not resolve secrets.

`run_automation(..., session_kernel=True)` makes an automation run in the same
kernel as `colab exec` (recording a new one in the session). It is used by
`secret set` and `hf login`; other automations keep a fresh kernel. Found in
the live test: without it the secret landed in a kernel `exec` never uses.

## Port forwarding (`commands/ssh.py`)

- `colab ssh -L SPEC` (repeatable), `SPEC` = `PORT` | `LOCAL:REMOTE` |
  `LOCAL:HOST:REMOTE`, host defaulting to `localhost` on the runtime. Works with
  the shell, `-- CMD`, and `-N/--no-shell` (forward only). `-L` adds
  `-o ExitOnForwardFailure=yes`. With `--proxy-mode` it is ignored with a
  pointer to `LocalForward`.
- `colab forward -s S PORT...` = `colab ssh -s S -N -L PORT...`.

## Keep-alive (`colab_cli/keepalive.py`, `Client.keep_alive_assignment`)

`Client.keep_alive_assignment(endpoint)` is restored from `05027b6`:
`GET /tun/m/<endpoint>/keep-alive/` with `X-Colab-Tunnel: Google`; a read
timeout is success. Commands that hold a forward or tunnel open ping it every
60 s **from their own foreground process**: no daemon and no background thread.

- `wait_with_keep_alive(proc, ...)`: with `-L`, ssh runs under `Popen` and the
  CLI pings on each `proc.wait(timeout=60)` expiry; Ctrl+C waits for ssh and
  returns 130. `--no-keep-alive` uses plain `subprocess.call`.
- `keep_alive_until_interrupted(...)`: used by `colab share` and
  `colab keepalive -s S [--minutes N]`.
- A failed ping warns once and keeps going; a 404 (runtime gone) stops pinging.

## Public tunnels (`commands/share.py`)

`colab share -s S PORT [--via cloudflared|ngrok] [--token SOURCE] [--detach]`,
`--list`, `--stop [PORT]`.

- The tunnel program runs **on the runtime** (detached `Popen`,
  `start_new_session=True`), recorded in `/tmp/colab-share-<port>.pid` as
  `<pid> <tool> <url>`; its log is `/tmp/colab-share-<port>.log`. Nothing runs
  locally after the command exits.
- Installation uses each vendor's signed apt repository, with the key scoped
  by `signed-by` and only that list refreshed: Cloudflare
  (`pkg.cloudflare.com/cloudflared any main`) and ngrok
  (`ngrok-agent.s3.amazonaws.com buster main`). apt verifies the package
  signatures, so no binary hash is pinned in the CLI.
- The URL is read from the tool's log: `https://*.trycloudflare.com` for a
  cloudflared quick tunnel (no account), `url=https://...` for ngrok.
- ngrok's authtoken (`NGROK_AUTHTOKEN`, `--token`, or a hidden prompt) reaches
  ngrok through the environment, not argv, and is redacted.
- Default: stay in the foreground keeping the runtime alive; Ctrl+C stops the
  share. `--detach` exits and points to `colab keepalive` and `--stop`.
- Every start warns that the port becomes public. Colab's terms may restrict
  tunnelling; that is the user's responsibility.

## Hugging Face (`commands/hf.py`)

- `colab hf login [--token SOURCE]`: default sources `env:HF_TOKEN`, `hf-cli`,
  `prompt`. Sets `HF_TOKEN` in the session kernel and runs
  `huggingface_hub.login(add_to_git_credential=False)`.
- `colab hf download REPO_ID [--repo-type model|dataset|space] [--revision R]
  [--local-dir DIR]`: `snapshot_download` on the runtime, values passed with
  `repr()`, timeout one hour. A missing `huggingface_hub` points to
  `colab install huggingface_hub`.

## GitHub (`commands/git.py`)

`colab clone REPO [DEST] [--branch B] [--depth N] [--token SOURCE|none]`.
`REPO` is `owner/name` (GitHub) or an `https://` URL; ssh/http URLs and URLs
with credentials are refused. `DEST` defaults to `/content/<name>`.

- For github.com the token defaults to `GH_TOKEN`, `GITHUB_TOKEN`, then
  `gh auth token`, if any (public repositories need none). Other hosts get a
  token only with an explicit `--token`.
- The token is sent as `http.extraHeader: Authorization: Basic
  base64(x-access-token:TOKEN)` set through `GIT_CONFIG_COUNT/KEY_0/VALUE_0`
  for that one git process: not in the URL, argv or `.git/config`. The token
  and its base64 form are redacted. `GIT_TERMINAL_PROMPT=0` prevents a hang.
- Only cloning is in scope; pushing, branches and PRs are not.

## Testing Strategy (TDD)

- `tests/test_secrets.py`, `tests/test_history.py` (redaction),
  `tests/test_secret_cmd.py`, `--secret` cases in `tests/test_exec.py` and
  `tests/test_run.py`.
- `tests/test_ssh_forward.py`, `tests/test_keepalive.py`, keep-alive cases in
  `tests/test_client.py`.
- `tests/test_hf.py`, `tests/test_git.py`, `tests/test_share.py`.

Live (Windows, CPU runtime, 2026-11-03): the keep-alive ping returned without
an HTTP error; `secret set` + `exec --secret` values were visible in the exec
kernel and absent from the history file (`***` instead); `clone
octocat/Hello-World --depth 1` worked and a missing repository exited 1;
`hf download hf-internal-testing/tiny-random-bert` worked; `colab ssh -L 8000
-- sleep 45` served the runtime's `http.server` at `localhost:8000`;
`colab share 8000 --detach` installed cloudflared, the public URL returned
HTTP 200, `--list` showed it and `--stop` made it return 530. Not run live:
the ngrok path (needs the user's authtoken), a private-repository clone and
`hf login` with a real token, and the interactive Ctrl+C paths of
`colab forward` / foreground `colab share`.
