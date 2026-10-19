"""打包與發行腳本裡不需要真的打包、上網的部分：版本號讀寫、發行前的防呆。"""
import os
import tempfile
import unittest
from unittest import mock

import build
import translator


class VersionTest(unittest.TestCase):
    def test_reads_same_version_as_package(self):
        self.assertEqual(build.read_version(), translator.__version__)

    def test_write_version_keeps_rest_of_file_and_line_endings(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "__init__.py")
            original = '"""說明"""\r\n__version__ = "1.0.0"  # 註解\r\n\r\nfrom .x import y\r\n'
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(original)
            with mock.patch.object(build, "VERSION_FILE", path):
                build.write_version("1.2.3")
                self.assertEqual(build.read_version(), "1.2.3")
            with open(path, encoding="utf-8", newline="") as f:
                self.assertEqual(f.read(), original.replace("1.0.0", "1.2.3"))

    def test_asset_name_is_ascii_and_versioned(self):
        name = os.path.basename(build.asset_path("1.2.3"))
        self.assertEqual(name, "MToolTranslator-v1.2.3-windows.zip")
        self.assertTrue(name.isascii())


class GuardTest(unittest.TestCase):
    def test_release_rejects_bad_version(self):
        with self.assertRaises(build.BuildError):
            build.release("1.2", dry_run=True)

    def test_publish_rejects_tag_that_does_not_match_version(self):
        with self.assertRaises(build.BuildError) as cm:
            build.publish("v0.0.1")
        self.assertIn("對不上", str(cm.exception))

    def test_publish_requires_built_zip(self):
        with mock.patch.object(build, "asset_path", lambda v: os.path.join(tempfile.gettempdir(), "沒有這個檔.zip")):
            with self.assertRaises(build.BuildError) as cm:
                build.publish(f"v{build.read_version()}")
        self.assertIn("找不到要上傳的 zip", str(cm.exception))

    def test_release_refuses_other_branch(self):
        with mock.patch.object(build, "_git", lambda *a: "feature" if a[0] == "rev-parse" else ""):
            with self.assertRaises(build.BuildError) as cm:
                build.release(dry_run=True)
        self.assertIn("main", str(cm.exception))

    def test_release_refuses_dirty_tree(self):
        answers = {"rev-parse": "main", "status": " M app.py"}
        with mock.patch.object(build, "_git", lambda *a: answers.get(a[0], "")):
            with self.assertRaises(build.BuildError) as cm:
                build.release(dry_run=True)
        self.assertIn("尚未 commit", str(cm.exception))

    def test_release_refuses_existing_tag(self):
        answers = {"rev-parse": "main", "status": "", "fetch": "", "rev-list": "0", "tag": "v1.0.0"}
        with mock.patch.object(build, "_git", lambda *a: answers.get(a[0], "")):
            with self.assertRaises(build.BuildError) as cm:
                build.release(dry_run=True)
        self.assertIn("已經發行過", str(cm.exception))

    def test_release_refuses_when_behind_remote(self):
        answers = {"rev-parse": "main", "status": "", "fetch": "", "rev-list": "2"}
        with mock.patch.object(build, "_git", lambda *a: answers.get(a[0], "")):
            with self.assertRaises(build.BuildError) as cm:
                build.release(dry_run=True)
        self.assertIn("落後", str(cm.exception))


class ReleaseFlowTest(unittest.TestCase):
    """模擬正式發行：記錄每個 git / gh 指令，不真的打包、不真的推送。"""

    def setUp(self):
        self.cmds, self.version = [], "1.0.0"
        answers = {"rev-parse": "main", "status": "", "fetch": "", "rev-list": "0", "tag": "", "ls-remote": ""}
        patches = [
            mock.patch.object(build, "_git", lambda *a: answers.get(a[0], "")),
            mock.patch.object(build, "run", lambda cmd, **kw: self.cmds.append(cmd[:3]) or 0),
            mock.patch.object(build, "read_version", lambda: self.version),
            mock.patch.object(build, "write_version", lambda v: setattr(self, "version", v)),
            mock.patch.object(build, "publish", lambda tag: self.cmds.append(["publish", tag])),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def pushed(self):
        return [c for c in self.cmds if c[:2] == ["git", "push"]]

    def test_full_release_order(self):
        with mock.patch.object(build, "gh_ready", lambda: True), mock.patch.object(build, "build", lambda **kw: None):
            build.release("1.1.0")
        self.assertEqual(self.version, "1.1.0")
        self.assertEqual(self.cmds, [["git", "add", build.VERSION_FILE], ["git", "commit", "-m"],
                                     ["git", "push", "origin"], ["git", "tag", "-a"], ["git", "push", "origin"],
                                     ["publish", "v1.1.0"]])

    def test_build_failure_pushes_nothing_and_rerun_does_not_recommit(self):
        def fail(**kw):
            raise build.BuildError("自我檢查沒過")
        with mock.patch.object(build, "gh_ready", lambda: True), mock.patch.object(build, "build", fail):
            with self.assertRaises(build.BuildError):
                build.release("1.1.0")
        self.assertEqual(self.pushed(), [])
        self.assertEqual(sum(c[:2] == ["git", "commit"] for c in self.cmds), 1)
        self.cmds.clear()
        with mock.patch.object(build, "gh_ready", lambda: True), mock.patch.object(build, "build", lambda **kw: None):
            build.release("1.1.0")  # 修正後用同一個版本號重跑
        self.assertFalse(any(c[:2] == ["git", "commit"] for c in self.cmds))
        self.assertEqual(len(self.pushed()), 2)

    def test_without_gh_pushes_tag_but_does_not_publish(self):
        with mock.patch.object(build, "gh_ready", lambda: False), mock.patch.object(build, "build", lambda **kw: None):
            build.release()
        self.assertEqual(len(self.pushed()), 2)
        self.assertFalse(any(c[0] == "publish" for c in self.cmds))

    def test_dry_run_changes_nothing(self):
        with mock.patch.object(build, "gh_ready", lambda: True), mock.patch.object(build, "build", lambda **kw: None):
            build.release("1.1.0", dry_run=True)
        self.assertEqual((self.cmds, self.version), ([], "1.0.0"))


if __name__ == "__main__":
    unittest.main()
