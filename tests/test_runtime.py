"""照實際啟動的方式跑：用預設設定位置建立主視窗、在獨立程序跑 --self-check。"""
import os
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock

import app
import runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RuntimeTest(unittest.TestCase):
    def test_app_dir_falls_back_when_not_writable(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(runtime, "_writable", lambda folder: folder != runtime.SOURCE_DIR), \
                mock.patch.dict(os.environ, {"APPDATA": d}):
            self.assertEqual(runtime.app_dir(), os.path.join(d, runtime.APP_ID))

    def test_resource_path_finds_icon(self):
        self.assertTrue(os.path.exists(runtime.resource_path(os.path.join("assets", "app.ico"))))

    def test_app_with_default_settings_location(self):
        """不指定設定檔路徑、跟雙擊啟動一樣建立主視窗。"""
        with tempfile.TemporaryDirectory() as d, mock.patch.object(app, "app_dir", lambda: d):
            try:
                root = tk.Tk()
            except tk.TclError as e:
                self.skipTest(str(e))
            root.withdraw()
            try:
                a = app.App(root)
                self.assertEqual(a.settings_path, os.path.join(d, "settings.json"))
                self.assertEqual(a.provider.id, "claude")
            finally:
                root.destroy()

    def test_self_check_in_separate_process(self):
        report = os.path.join(ROOT, "self-check.txt")
        try:
            r = subprocess.run([sys.executable, "app.py", "--self-check"], cwd=ROOT, timeout=120)
            with open(report, encoding="utf-8") as f:
                text = f.read()
            self.assertEqual(r.returncode, 0, text)
            self.assertIn("OK    主視窗啟動", text)
        finally:
            if os.path.exists(report):
                os.remove(report)


if __name__ == "__main__":
    unittest.main()
