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

import os
import tempfile
import shutil
import unittest
from colab_cli.history import HistoryLogger


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.logger = HistoryLogger(log_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_log_and_get_history(self):
        self.logger.log_event("test-session", "session_created", {"variant": "DEFAULT"})
        self.logger.log_event(
            "test-session", "execution", {"code": "print(1)", "outputs": []}
        )

        history = self.logger.get_history("test-session")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["event_type"], "session_created")
        self.assertEqual(history[1]["event_type"], "execution")
        self.assertEqual(history[1]["code"], "print(1)")

    def test_list_sessions(self):
        self.logger.log_event("s1", "event", {})
        self.logger.log_event("s2", "event", {})

        sessions = self.logger.list_sessions()
        self.assertIn("s1", sessions)
        self.assertIn("s2", sessions)
        self.assertEqual(len(sessions), 2)

    def test_redacted_values_never_reach_the_log_file(self):
        secret = "hf_SuperSecret123"
        self.logger.add_redaction(secret)
        code = f"import os\nos.environ['HF_TOKEN'] = {secret!r}\n"
        self.logger.log_event(
            "s", "execution", {"code": code, "outputs": [{"text": secret}]}
        )
        with open(os.path.join(self.test_dir, "s.jsonl"), encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(secret, raw)
        event = self.logger.get_history("s")[0]
        self.assertIn("***", event["code"])
        self.assertEqual(event["outputs"][0]["text"], "***")

    def test_redaction_covers_json_and_repr_escaped_forms(self):
        secret = "tok\"en\\with'quotes"
        self.logger.add_redaction(secret)
        self.logger.log_event("s", "execution", {"code": repr(secret), "raw": secret})
        event = self.logger.get_history("s")[0]
        self.assertNotIn("en\\with", event["code"])
        self.assertNotIn("en\\with", event["raw"])

    def test_empty_redaction_is_ignored(self):
        self.logger.add_redaction("")
        self.logger.log_event("s", "event", {"x": "abc"})
        self.assertEqual(self.logger.get_history("s")[0]["x"], "abc")


if __name__ == "__main__":
    unittest.main()
