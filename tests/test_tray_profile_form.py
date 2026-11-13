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

"""The profile editor's form logic and help texts, without Qt (docs/09)."""

import pytest

from colab_cli.profiles import Profile
from colab_cli.tray.i18n import STRINGS
from colab_cli.tray.profile_form import FIELDS, FormError, from_form, hardware_choices, to_form, valid_name


def test_round_trip_job_and_kernel():
    job = Profile(script="t.py", args=["--name", "my run"], gpu="T4", budget=2.0, secrets=["HF_TOKEN", "W"],
                  env={"A": "1", "B": "x=y"}, description="d")
    assert from_form(to_form(job)) == job
    kernel = Profile(script="s.py", mode="kernel", session="ui", tpu="V5E1", vars={"C": {"a": [1, 2]}},
                     forward=["8188", "8080:80"], ssh_key="~/k", open="http://x", timeout=60, stop_when_done=False)
    assert from_form(to_form(kernel)) == kernel


def test_new_profile_defaults():
    form = to_form(None)
    assert form["mode"] == "job" and form["hardware"] == "CPU" and form["stop_when_done"] is True


def test_fields_of_the_other_mode_are_dropped():
    form = to_form(Profile(script="s.py", mode="kernel", forward=["1"], vars={"A": 1}))
    form["mode"] = "job"
    p = from_form(form)
    assert p.forward == [] and p.vars == {}


@pytest.mark.parametrize("change, field", [
    ({"script": ""}, "script"),
    ({"env": "NOEQUALS"}, "env"),
    ({"budget": "lots"}, "budget"),
    ({"budget": "0"}, "budget"),
    ({"args": '"unterminated'}, "args"),
    ({"mode": "kernel", "vars": "[1]"}, "vars"),
    ({"mode": "kernel", "vars": "{bad"}, "vars"),
    ({"mode": "kernel", "forward": "abc"}, "forward"),
])
def test_errors_name_the_field(change, field):
    form = {**to_form(Profile(script="t.py")), **change}
    with pytest.raises(FormError) as e:
        from_form(form)
    assert e.value.field == field


def test_budget_accepts_a_decimal_comma():
    assert from_form({**to_form(Profile(script="t.py")), "budget": "1,5"}).budget == 1.5


def test_hardware_choices_and_names():
    choices = hardware_choices()
    assert choices[0] == "CPU" and "T4" in choices and "TPU V5E1" in choices
    assert valid_name("train-t4.v2") and not valid_name("a b") and not valid_name("")


def test_every_field_has_a_label_and_help_in_both_languages():
    assert set(STRINGS["en"]) == set(STRINGS["hu"])
    for lang in ("en", "hu"):
        for field, _, _ in FIELDS:
            assert STRINGS[lang].get(f"field.{field}"), (lang, field)
            assert STRINGS[lang].get(f"help.{field}"), (lang, field)
