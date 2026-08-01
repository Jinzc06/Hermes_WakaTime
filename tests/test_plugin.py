"""Unit tests for the wakatime plugin's pure logic (no network, no Hermes).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MODULE_NAME = "wakatime_test_module"


def load_plugin():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class LanguageDetectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin()

    def test_python(self):
        self.assertEqual(self.plugin._language_for("/repo/src/app.py"), "Python")

    def test_typescript_tsx(self):
        self.assertEqual(self.plugin._language_for("Component.tsx"), "TSX")
        self.assertEqual(self.plugin._language_for("util.ts"), "TypeScript")

    def test_markdown_and_text(self):
        self.assertEqual(self.plugin._language_for("README.md"), "Markdown")
        self.assertEqual(self.plugin._language_for("notes.txt"), "Text")

    def test_unknown_extension(self):
        self.assertIsNone(self.plugin._language_for("/repo/blob.zzz"))

    def test_filename_languages(self):
        self.assertEqual(self.plugin._language_for("/repo/Dockerfile"), "Dockerfile")
        self.assertEqual(self.plugin._language_for("/repo/Makefile"), "Makefile")

    def test_case_insensitive(self):
        self.assertEqual(self.plugin._language_for("/repo/app.PY"), "Python")

    def test_category(self):
        self.assertEqual(self.plugin._category_for("Python"), "coding")
        self.assertEqual(self.plugin._category_for("Markdown"), "writing")
        self.assertEqual(self.plugin._category_for(None), "coding")


class ProjectDetectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin()

    def test_git_root_detection(self):
        with tempfile.TemporaryDirectory(prefix="wt-proj-") as tmp:
            repo = Path(tmp) / "myrepo"
            (repo / ".git").mkdir(parents=True)
            (repo / "src").mkdir()
            project = self.plugin._project_for(str(repo / "src" / "main.py"))
            self.assertEqual(project, "myrepo")

    def test_no_git_falls_back_to_dir_name(self):
        with tempfile.TemporaryDirectory(prefix="wt-plain-") as tmp:
            project = self.plugin._project_for(tmp)
            self.assertEqual(project, Path(tmp).name)

    def test_env_override(self):
        os.environ["HERMES_WAKATIME_PROJECT"] = "forced-project"
        try:
            self.assertEqual(self.plugin._project_for("/tmp/whatever/x.py"), "forced-project")
        finally:
            del os.environ["HERMES_WAKATIME_PROJECT"]

    def test_project_cache_is_bounded(self):
        self.plugin._project_cache.clear()
        for i in range(self.plugin._MAX_PROJECT_CACHE + 100):
            self.plugin._project_from_dir(f"/tmp/cache-test-{i}")
        self.assertLessEqual(len(self.plugin._project_cache), self.plugin._MAX_PROJECT_CACHE)


class AuthEncodingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin()

    def test_basic_encodings(self):
        import base64

        key = "test-key"
        encodings = self.plugin._auth_encodings(key)
        self.assertEqual(encodings[0], base64.b64encode(b"test-key").decode())
        self.assertEqual(encodings[1], base64.b64encode(b"test-key:").decode())


class HeartbeatThrottleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin()

    def setUp(self):
        self.plugin._last_sent.clear()
        self._old_key = os.environ.get("HERMES_WAKATIME_API_KEY")
        os.environ["HERMES_WAKATIME_API_KEY"] = "test-key"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("HERMES_WAKATIME_API_KEY", None)
        else:
            os.environ["HERMES_WAKATIME_API_KEY"] = self._old_key

    def test_throttle_blocks_duplicate_entity(self):
        self.plugin._last_sent[("proj", "file.py")] = 0.0  # sent "long ago"
        sent = self.plugin._emit("file.py", project="proj")
        self.assertTrue(sent)
        again = self.plugin._emit("file.py", project="proj")  # within 60s window
        self.assertFalse(again)

    def test_different_entities_not_throttled(self):
        self.assertTrue(self.plugin._emit("a.py", project="proj"))
        self.assertTrue(self.plugin._emit("b.py", project="proj"))

    def test_no_key_returns_false(self):
        self.plugin._warned_no_key = True  # silence logging
        old = os.environ.get("HERMES_WAKATIME_API_KEY")
        os.environ.pop("HERMES_WAKATIME_API_KEY", None)
        try:
            self.assertFalse(self.plugin._emit("x.py", project="p"))
        finally:
            if old is not None:
                os.environ["HERMES_WAKATIME_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
