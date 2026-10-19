"""介面驗收：開真的視窗，接本機假伺服器，從測試連線一路操作到翻譯、停止、續翻、關閉。"""
import gc
import json
import logging
import os
import tempfile
import time
import tkinter as tk
import tkinter.ttk
import unittest
from unittest import mock

import app
from tests.test_engines import ServerTest
from translator import PROVIDER_BY_ID, read_json

logging.disable(logging.CRITICAL)


class AppTest(ServerTest):
    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"沒有可用的視窗環境：{e}")
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "ManualTransFile.json")
        entries = ["はい", "いいえ", "\\C[2]アリス\\C[0]が来た", "100"] + [f"はい{i}" for i in range(40)]
        with open(self.src, "w", encoding="utf-8") as f:
            json.dump({k: k for k in entries}, f, ensure_ascii=False)
        self.out = os.path.join(self.tmp.name, "ManualTransFile_translated.json")
        self.dialogs = []
        patches = {name: mock.patch.object(app.messagebox, name, self._dialog(name))
                   for name in ("showinfo", "showerror", "showwarning", "askyesno")}
        for p in patches.values():
            p.start()
            self.addCleanup(p.stop)
        self.settings_path = os.path.join(self.tmp.name, "settings.json")
        self.app = app.App(self.root, self.settings_path)

    def tearDown(self):
        try:
            for after_id in self.root.tk.call("after", "info"):
                self.root.after_cancel(after_id)
            self.root.destroy()
        except tk.TclError:
            pass
        if self.app.worker:
            self.app.worker.join(10)
        # 視窗物件有循環參照；在主執行緒回收掉，免得垃圾回收剛好發生在背景執行緒而出現 Tk 警告
        del self.app, self.root
        gc.collect()
        self.tmp.cleanup()

    def find_button(self, parent, text):
        for w in parent.winfo_children():
            if isinstance(w, tk.ttk.Button) and w.cget("text") == text:
                return w
            found = self.find_button(w, text)
            if found:
                return found

    def _dialog(self, name):
        def f(title, msg="", **kw):
            self.dialogs.append((name, title, msg))
            return True
        return f

    def pump(self, cond, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if cond():
                return
            time.sleep(0.01)
        self.fail("等待逾時")

    def use_fake_llm(self, model="m"):
        a = self.app
        a.cat_var.set("雲端 API")
        a._on_category()
        a.provider_var.set(PROVIDER_BY_ID["custom"].name)
        a._on_provider_selected()
        a.url_var.set(self.url + "/v1")
        a.model_var.set(model)
        a._set_input(self.src)

    def state(self, w):
        return str(w.cget("state"))

    # ------------------------------------------------------------

    def test_first_run_shows_guide_and_remembers(self):
        self.pump(lambda: any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()), 3)
        guide = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        self.find_button(guide, "我知道了").invoke()
        self.assertFalse(guide.winfo_exists())
        self.assertTrue(read_json(self.settings_path)["guide_seen"])

    def test_field_states_follow_provider_and_values_are_remembered(self):
        a = self.app
        a.key_var.set("sk-claude")
        a.cat_var.set("免費服務")
        a._on_category()
        self.assertEqual(a.provider.id, "mymemory")
        self.assertEqual(self.state(a.url_ent), "disabled")
        self.assertEqual(self.state(a.model_cb), "disabled")
        self.assertEqual(self.state(a.extract_btn), "disabled")
        self.assertIn("Email", a.key_lbl.cget("text"))
        a.cat_var.set("雲端 API")
        a._on_category()
        self.assertEqual((a.provider.id, a.key_var.get()), ("claude", "sk-claude"))
        self.assertEqual(self.state(a.extract_btn), "normal")

    def test_full_flow_test_models_terms_translate(self):
        a = self.app
        self.use_fake_llm()
        self.assertIn("測試連線", a.next_lbl.cget("text"))

        a._test_engine()
        self.pump(lambda: a.tested_provider == "custom")
        self.assertIn("連線成功", self.dialogs[-1][2])
        self.assertIn("自動擷取術語", a.next_lbl.cget("text"))

        a._fetch_models()
        self.pump(lambda: tuple(a.model_cb["values"]) == ("a-model", "b-model"))

        a._extract()
        self.pump(lambda: a.job is None and a.terms_extracted)
        self.assertEqual(a.glossary, {"アリス": "愛麗絲"})
        self.assertEqual(read_json(self.src.replace(".json", ".glossary.json")), {"アリス": "愛麗絲"})

        a._start_translate()
        self.assertEqual(self.state(a.start_btn), "disabled")
        self.assertEqual(self.state(a.url_ent), "disabled")
        self.pump(lambda: a.job is None and a.translated_once)
        res = read_json(self.out)
        self.assertEqual(res["\\C[2]アリス\\C[0]が来た"], "\\C[2]愛麗絲\\C[0]來了")
        self.assertEqual(res["100"], "100")
        self.assertEqual(a.last_progress.done, a.last_progress.total)
        self.assertIn("100.0%", a.pct_lbl.cget("text"))
        self.assertIn("加載翻譯文件", a.next_lbl.cget("text"))
        self.assertEqual(self.state(a.start_btn), "normal")
        self.assertEqual(self.dialogs[-1][1], "翻譯完成")

    def test_stop_then_resume(self):
        a = self.app
        self.use_fake_llm("slow")
        a.batch_var.set(1)
        a.workers_var.set(1)
        a._start_translate()
        self.pump(lambda: a.last_progress and a.last_progress.done >= 5)
        a._stop()
        self.pump(lambda: a.job is None)
        self.assertIn("已停止", a.phase_lbl.cget("text"))
        stopped_at = a.last_progress.done
        self.assertLess(stopped_at, a.last_progress.total)
        self.assertEqual(a.last_progress.failed, 0)

        a.model_var.set("m")
        a._start_translate()
        self.pump(lambda: a.job is None)
        self.assertGreaterEqual(min(p for p in [a.last_progress.done]), stopped_at)
        self.assertEqual(a.last_progress.done, a.last_progress.total)
        self.assertNotIn("はい", [v for k, v in read_json(self.out).items() if k.startswith("はい")])

    def test_fatal_error_unlocks_and_reports(self):
        a = self.app
        self.use_fake_llm("auth")
        a._start_translate()
        self.pump(lambda: a.job is None)
        self.assertEqual(self.dialogs[-1][:2], ("showerror", "已中止"))
        self.assertIn("金鑰無效", self.dialogs[-1][2])
        self.assertEqual(self.state(a.start_btn), "normal")
        self.assertIn("已中止", a.phase_lbl.cget("text"))

    def test_close_during_job_waits_for_save(self):
        a = self.app
        self.use_fake_llm("slow")
        a.batch_var.set(1)
        a.workers_var.set(1)
        a._start_translate()
        self.pump(lambda: a.last_progress and a.last_progress.done >= 3)
        destroyed = []
        with mock.patch.object(self.root, "destroy", lambda: destroyed.append(1)):
            a._on_close()
            self.pump(lambda: destroyed)
        self.assertFalse(a.worker.is_alive())
        saved = read_json(self.out)
        self.assertGreaterEqual(sum(1 for k, v in saved.items() if v != k), 3)

    def test_input_validation(self):
        a = self.app
        a._start_translate()
        self.assertEqual(self.dialogs[-1][1], "缺少原文檔")
        self.use_fake_llm()
        a.out_var.set(os.path.join(self.tmp.name, "沒有這個資料夾", "o.json"))
        a._start_translate()
        self.assertEqual(self.dialogs[-1][1], "資料夾不存在")
        a.url_var.set("不是網址")
        a.out_var.set(self.out)
        a._start_translate()
        self.assertEqual(self.dialogs[-1][1], "引擎設定錯誤")
        self.assertIsNone(a.job)

    def test_tooltips_render_dynamic_text(self):
        from ui_widgets import Tooltip
        t = Tooltip(self.app.provider_cb, lambda: self.app.provider.help, delay=0)
        t._show()
        label = t._tip.winfo_children()[0]
        self.assertEqual(label.cget("text").replace(" ", " "), self.app.provider.help)
        t._hide()
        self.assertIsNone(t._tip)

    def test_corrupt_glossary_does_not_crash(self):
        with open(self.src.replace(".json", ".glossary.json"), "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        self.app._set_input(self.src)
        self.assertEqual(self.app.glossary, {})
        self.assertEqual(self.dialogs[-1][1], "術語表讀取失敗")


if __name__ == "__main__":
    unittest.main()
