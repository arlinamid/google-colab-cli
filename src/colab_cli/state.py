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

import contextlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, Iterator, IO

import filelock
from pydantic import BaseModel


class SessionState(BaseModel):
    name: str
    token: str
    url: str
    endpoint: str
    token_expires_at: Optional[datetime] = None
    variant: str = "DEFAULT"
    accelerator: str = "NONE"
    machine_shape: str = "STANDARD"
    kernel_id: Optional[str] = None
    session_id: Optional[str] = None
    last_execution: Optional[Tuple[str, Optional[str], str]] = None
    running: Optional[str] = None
    # What a foreground `colab exec` is running now: file, cell (from 1),
    # total, label and started_at (ISO). Read by `colab status` and the tray
    # without touching the runtime.
    activity: Optional[Dict[str, Any]] = None
    # Set by `colab profile run`: the profile, and whether the tray stops the
    # runtime once its job has ended.
    profile: Optional[str] = None
    stop_when_done: Optional[bool] = None


class Settings(BaseModel):
    # Google's official package: only announced, never installed by this fork.
    update_url: str = "https://pypi.org/pypi/google-colab-cli/json"
    # This fork's latest GitHub release: the source of installable updates.
    fork_release_url: str = (
        "https://api.github.com/repos/arlinamid/google-colab-cli/releases/latest"
    )
    last_check: Optional[datetime] = None
    enable_update_check: bool = True
    # Highest fork version seen on the update source; cached for the banner.
    latest_version: Optional[str] = None
    # Wheel of that fork release, installed by `colab update --install`.
    latest_wheel_url: Optional[str] = None
    # `colab tray` (docs/09): refresh interval, notifications, panel language
    # ("auto", "hu", "en") and the profile its main button starts.
    tray_interval: Optional[float] = None
    tray_notifications: bool = True
    tray_language: str = "auto"
    tray_last_profile: Optional[str] = None


_OWNER_ONLY_MODE = 0o600


def private_opener(path: str, flags: int) -> int:
    """Opens `path` for `open(opener=...)` and returns its file descriptor.

    New files are created readable only by the owner.
    """
    return os.open(path, flags, _OWNER_ONLY_MODE)


def make_private(path: str) -> None:
    """Restricts an existing file to owner-only access, ignoring errors."""
    with contextlib.suppress(OSError):
        os.chmod(path, _OWNER_ONLY_MODE)


class _LockedFileStore:
    def __init__(self, path: str):
        self.path = path
        self.lock_path = "%s.lock" % self.path
        # ReadWriteLock gives us shared (concurrent) readers and exclusive
        # writers -- the cross-platform equivalent of fcntl LOCK_SH/LOCK_EX.
        # is_singleton=False keeps each store's lock independent: with the
        # default (True), two StateStore instances for the same path in one
        # process are merged into a single reentrant lock, whose reentrancy
        # guard then raises RuntimeError when two threads contend for the write
        # lock. We want them to actually serialize via the underlying file lock.
        self._rwlock = filelock.ReadWriteLock(self.lock_path, is_singleton=False)
        self._ensure_dir()
        make_private(self.path)

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _write_data(self, f: IO, data: str):
        f.seek(0)
        f.truncate()
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    @contextlib.contextmanager
    def _lock_shared(self) -> Iterator[Optional[IO]]:
        if not os.path.exists(self.path):
            yield None
            return
        with self._rwlock.read_lock():
            with open(self.path, "r") as f:
                yield f

    @contextlib.contextmanager
    def _lock_exclusive(self) -> Iterator[IO]:
        with self._rwlock.write_lock():
            with open(self.path, "a+", opener=private_opener) as f:
                yield f


class SettingsStore(_LockedFileStore):
    def __init__(self, path: Optional[str] = None):
        if not path:
            path = os.path.expanduser("~/.config/colab-cli/settings.json")
        super().__init__(path)

    def load(self) -> Settings:
        with self._lock_shared() as f:
            if f is None:
                return Settings()
            try:
                content = f.read()
                if not content or content.isspace():
                    return Settings()
                data = json.loads(content)
                return Settings.model_validate(data)
            except Exception:
                return Settings()

    def save(self, settings: Settings):
        with self._lock_exclusive() as f:
            self._write_data(f, settings.model_dump_json(indent=2))


class StateStore(_LockedFileStore):
    def __init__(self, path: Optional[str] = None):
        if not path:
            path = os.path.expanduser("~/.config/colab-cli/sessions.json")
        super().__init__(path)

    def _load_raw(self, f) -> Dict[str, SessionState]:
        try:
            f.seek(0)
            content = f.read()
            if not content or content.isspace():
                return {}
            data = json.loads(content)
            return {k: SessionState(**v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_raw(self, f, sessions: Dict[str, SessionState]):
        content = json.dumps(
            {k: v.model_dump(mode="json") for k, v in sessions.items()}, indent=2
        )
        self._write_data(f, content)

    def add(self, state: SessionState):
        with self._lock_exclusive() as f:
            sessions = self._load_raw(f)
            sessions[state.name] = state
            self._save_raw(f, sessions)

    def update_if_present(self, state: SessionState) -> bool:
        """Write ``state`` back only if its session is still recorded.

        Long-running commands save the session again when they finish (to
        clear ``running``). If ``colab stop`` removed the session meanwhile, a
        plain ``add`` would resurrect a stopped session in sessions.json.
        Returns True when the session was updated.
        """
        with self._lock_exclusive() as f:
            sessions = self._load_raw(f)
            if state.name not in sessions:
                return False
            sessions[state.name] = state
            self._save_raw(f, sessions)
            return True

    def get(self, name: str) -> Optional[SessionState]:
        with self._lock_shared() as f:
            if f is None:
                return None
            sessions = self._load_raw(f)
            return sessions.get(name)

    def remove(self, name: str):
        with self._lock_exclusive() as f:
            sessions = self._load_raw(f)
            if name in sessions:
                del sessions[name]
                self._save_raw(f, sessions)

    def list(self) -> Dict[str, SessionState]:
        with self._lock_shared() as f:
            if f is None:
                return {}
            return self._load_raw(f)
