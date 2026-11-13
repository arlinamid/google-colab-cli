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

"""Run profiles: the profiles.toml file and its model (docs/09)."""

import os
import stat
import sys
import tomllib

import pytest

from colab_cli.profiles import (
    Profile,
    ProfileError,
    dump_toml,
    load_profiles,
    profiles_path,
    remove_profile,
    resolve_script,
    save_profile,
)


def _write(tmp_path, text):
    path = tmp_path / "profiles.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_missing_file_has_no_profiles(tmp_path):
    assert load_profiles(str(tmp_path / "profiles.toml")) == {}


def test_job_and_service_profiles(tmp_path):
    path = _write(tmp_path, """
# a comment
[train-t4]
script = "train.py"
args = ["--epochs", "3"]
gpu = "t4"
budget = 2.0
secrets = ["HF_TOKEN"]
env = { WANDB_MODE = "offline" }

[web-ui]
script = "server.py"
mode = "kernel"
session = "webui"
vars = { CONFIG = { access = "forward", port = 7860 } }
forward = [7860, "8080:80"]
open = "http://127.0.0.1:7860"
stop_when_done = false
""")
    profiles = load_profiles(path)

    train = profiles["train-t4"]
    assert train.mode == "job" and train.gpu == "T4" and train.budget == 2.0
    assert train.stop_when_done is True
    assert train.env == {"WANDB_MODE": "offline"}
    assert resolve_script(train, path) == os.path.join(str(tmp_path), "train.py")

    web = profiles["web-ui"]
    assert web.mode == "kernel" and web.session == "webui"
    assert web.vars == {"CONFIG": {"access": "forward", "port": 7860}}
    assert web.forward == ["7860", "8080:80"]
    assert web.ready_url == "http://127.0.0.1:7860"  # defaults to `open`
    assert web.stop_when_done is False


@pytest.mark.parametrize("body, needle", [
    ('script = "x.py"\nscirpt = "y.py"', "scirpt"),
    ('script = "x.py"\ngpu = "Z9"', "gpu"),
    ('script = "x.py"\ngpu = "T4"\ntpu = "V5E1"', "gpu"),
    ('script = "x.py"\nbudget = 0', "budget"),
    ('script = "x.py"\nvars = { A = 1 }', "kernel"),
    ('script = "x.py"\nmode = "kernel"\nbudget = 1', "budget"),
    ('script = "x.py"\nmode = "kernel"\nargs = ["a"]', "args"),
    ('script = "x.py"\nvars = { "1x" = 1 }\nmode = "kernel"', "1x"),
    ('script = "x.py"\nforward = ["abc"]', "forward"),
    ('args = []', "script"),
])
def test_invalid_profiles_name_the_problem(tmp_path, body, needle):
    path = _write(tmp_path, f"[p]\n{body}\n")
    with pytest.raises(ProfileError) as e:
        load_profiles(path)
    assert "'p'" in str(e.value) and needle in str(e.value)


def test_toml_syntax_error(tmp_path):
    path = _write(tmp_path, "[p\nscript = 1\n")
    with pytest.raises(ProfileError) as e:
        load_profiles(path)
    assert "profiles.toml" in str(e.value)


def test_dump_toml_round_trips():
    data = {
        "a b": {"script": "C:\\work\\t.py", "args": ["--x", 'say "hi"'], "high_mem": True,
                "budget": 1.5, "env": {"K": "v"}, "vars": {"C": {"n": 2, "ok": False, "l": [1, 2]}}},
        "plain": {"script": "s.py"},
    }
    assert tomllib.loads(dump_toml(data)) == data


def test_save_and_remove_keep_the_file_private(tmp_path):
    path = str(tmp_path / "sub" / "profiles.toml")
    save_profile(path, "one", Profile(script="a.py"))
    save_profile(path, "two", Profile(script="b.py", gpu="T4", stop_when_done=False))

    profiles = load_profiles(path)
    assert set(profiles) == {"one", "two"}
    assert profiles["two"].gpu == "T4" and profiles["two"].stop_when_done is False
    # Defaults are not written out.
    assert "stop_when_done" not in tomllib.loads(open(path, encoding="utf-8").read())["one"]
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    assert remove_profile(path, "one") is True
    assert remove_profile(path, "one") is False
    assert set(load_profiles(path)) == {"two"}


def test_profiles_path_follows_config(tmp_path):
    assert profiles_path(str(tmp_path / "s.json")) == str(tmp_path / "profiles.toml")
    assert profiles_path(None).endswith(os.path.join(".config", "colab-cli", "profiles.toml"))
