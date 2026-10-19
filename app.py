"""MTool 翻譯器 UI：python app.py"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import core

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")

# 類別 → 服務 → 預設值。kind: openai / claude / google / mymemory
PRESETS = {
    "雲端 API": {
        "Claude（Anthropic）": dict(kind="claude", model="claude-opus-5-5", workers=4, batch=25),
        "OpenAI": dict(kind="openai", base_url="https://api.openai.com/v1", model="gpt-4.1-mini", workers=4, batch=25),
        "Google Gemini": dict(kind="openai", base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                              model="gemini-2.5-flash", workers=4, batch=25),
        "DeepSeek": dict(kind="openai", base_url="https://api.deepseek.com/v1", model="deepseek-chat", workers=4, batch=25),
        "自訂（OpenAI 相容）": dict(kind="openai", base_url="https://", model="", workers=2, batch=20),
    },
    "免費服務": {
        "MyMemory": dict(kind="mymemory", key_label="Email（選填）", workers=2, batch=10),
        "Google 翻譯（非官方）": dict(kind="google", workers=2, batch=10),
    },
    "本地 AI": {
        "Sakura / llama.cpp": dict(kind="openai", base_url="http://localhost:8080/v1", model="sakura",
                                   temperature=0.1, workers=1, batch=10),
    },
    "本地 LLM": {
        "Ollama": dict(kind="openai", base_url="http://localhost:11434/v1", model="qwen2.5:14b",
                       temperature=0.2, workers=1, batch=10),
        "LM Studio": dict(kind="openai", base_url="http://localhost:1234/v1", model="",
                          temperature=0.2, workers=1, batch=10),
    },
}
SRC_LANGS = ["ja", "en", "zh-CN", "ko"]
TGT_LANGS = ["zh-TW", "zh-CN", "en", "ja"]


def fmt_time(sec):
    if sec is None:
        return "--:--:--"
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.glossary = {}
        self.term_keys = []
        self.cur_preset = ""
        self.s = self._load_settings()

        root.title("MTool 翻譯器")
        root.geometry("900x780")
        root.minsize(760, 640)
        self._build()
        self._restore()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

    # ------------------------------------------------------------ 版面

    def _build(self):
        pad = dict(padx=6, pady=3)
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # 檔案
        f = ttk.LabelFrame(main, text="檔案", padding=6)
        f.grid(row=0, column=0, sticky="ew")
        f.columnconfigure(1, weight=1)
        self.in_var, self.out_var = tk.StringVar(), tk.StringVar()
        ttk.Label(f, text="原文檔（MTool 匯出）").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.in_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="瀏覽…", command=self._pick_input).grid(row=0, column=2, **pad)
        ttk.Label(f, text="譯文輸出").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="瀏覽…", command=self._pick_output).grid(row=1, column=2, **pad)

        # 引擎
        e = ttk.LabelFrame(main, text="翻譯引擎", padding=6)
        e.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        e.columnconfigure(1, weight=1)
        e.columnconfigure(3, weight=1)
        self.cat_var = tk.StringVar(value="雲端 API")
        cats = ttk.Frame(e)
        cats.grid(row=0, column=0, columnspan=4, sticky="w", **pad)
        for c in PRESETS:
            ttk.Radiobutton(cats, text=c, value=c, variable=self.cat_var,
                            command=self._on_category).pack(side="left", padx=(0, 14))
        self.preset_var = tk.StringVar()
        ttk.Label(e, text="服務").grid(row=1, column=0, sticky="w", **pad)
        self.preset_cb = ttk.Combobox(e, textvariable=self.preset_var, state="readonly")
        self.preset_cb.grid(row=1, column=1, sticky="ew", **pad)
        self.preset_cb.bind("<<ComboboxSelected>>", lambda _: self._on_preset())
        self.hint = ttk.Label(e, foreground="#666")
        self.hint.grid(row=1, column=2, columnspan=2, sticky="w", **pad)

        self.url_var, self.key_var, self.model_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.url_lbl = ttk.Label(e, text="API 位址")
        self.url_lbl.grid(row=2, column=0, sticky="w", **pad)
        self.url_ent = ttk.Entry(e, textvariable=self.url_var)
        self.url_ent.grid(row=2, column=1, columnspan=3, sticky="ew", **pad)
        self.key_lbl = ttk.Label(e, text="API Key")
        self.key_lbl.grid(row=3, column=0, sticky="w", **pad)
        self.key_ent = ttk.Entry(e, textvariable=self.key_var, show="•")
        self.key_ent.grid(row=3, column=1, columnspan=3, sticky="ew", **pad)
        self.model_lbl = ttk.Label(e, text="模型")
        self.model_lbl.grid(row=4, column=0, sticky="w", **pad)
        self.model_cb = ttk.Combobox(e, textvariable=self.model_var)
        self.model_cb.grid(row=4, column=1, sticky="ew", **pad)
        btns = ttk.Frame(e)
        btns.grid(row=4, column=2, columnspan=2, sticky="w")
        self.models_btn = ttk.Button(btns, text="取得模型清單", command=self._fetch_models)
        self.models_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="測試連線", command=self._test_engine).pack(side="left", padx=4)

        nums = ttk.Frame(e)
        nums.grid(row=5, column=0, columnspan=4, sticky="w", **pad)
        self.workers_var, self.batch_var = tk.IntVar(value=1), tk.IntVar(value=10)
        ttk.Label(nums, text="同時請求數").pack(side="left")
        ttk.Spinbox(nums, from_=1, to=32, width=5, textvariable=self.workers_var).pack(side="left", padx=(4, 16))
        ttk.Label(nums, text="每批條數").pack(side="left")
        ttk.Spinbox(nums, from_=1, to=100, width=5, textvariable=self.batch_var).pack(side="left", padx=4)

        # 翻譯設定
        t = ttk.LabelFrame(main, text="翻譯設定", padding=6)
        t.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        t.columnconfigure(1, weight=1)
        row = ttk.Frame(t)
        row.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.src_var, self.tgt_var, self.ctx_var = tk.StringVar(value="ja"), tk.StringVar(value="zh-TW"), tk.IntVar(value=5)
        ttk.Label(row, text="來源").pack(side="left")
        ttk.Combobox(row, textvariable=self.src_var, values=SRC_LANGS, width=7, state="readonly").pack(side="left", padx=(4, 16))
        ttk.Label(row, text="目標").pack(side="left")
        ttk.Combobox(row, textvariable=self.tgt_var, values=TGT_LANGS, width=7, state="readonly").pack(side="left", padx=(4, 16))
        ttk.Label(row, text="前文參考行數").pack(side="left")
        ttk.Spinbox(row, from_=0, to=30, width=5, textvariable=self.ctx_var).pack(side="left", padx=4)
        ttk.Label(t, text="遊戲背景").grid(row=1, column=0, sticky="nw", **pad)
        self.bg_text = tk.Text(t, height=3, wrap="word", font="TkDefaultFont")
        self.bg_text.grid(row=1, column=1, sticky="ew", **pad)
        ttk.Label(t, text="例：奇幻 RPG，主角アリス是女騎士，說話直率；語氣輕鬆。只有 LLM 引擎會用到。",
                  foreground="#666").grid(row=2, column=1, sticky="w", padx=6)

        # 術語表 / 記錄
        nb = ttk.Notebook(main)
        nb.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        main.rowconfigure(3, weight=1)

        g = ttk.Frame(nb, padding=4)
        nb.add(g, text="術語表")
        g.columnconfigure(0, weight=1)
        g.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(g, columns=("src", "dst"), show="headings", selectmode="extended")
        self.tree.heading("src", text="原文")
        self.tree.heading("dst", text="譯名")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _: self._edit_term())
        sb = ttk.Scrollbar(g, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        gb = ttk.Frame(g)
        gb.grid(row=0, column=2, sticky="n", padx=(6, 0))
        for text, cmd in [("新增", self._add_term), ("編輯", self._edit_term), ("刪除", self._del_terms),
                          (None, None), ("自動擷取術語", self._extract), (None, None),
                          ("匯入…", self._import_terms), ("匯出…", self._export_terms)]:
            if text is None:
                ttk.Separator(gb).pack(fill="x", pady=6)
            else:
                ttk.Button(gb, text=text, command=cmd).pack(fill="x", pady=2)
        self.term_count = ttk.Label(gb, foreground="#666")
        self.term_count.pack(pady=6)

        lg = ttk.Frame(nb, padding=4)
        nb.add(lg, text="記錄")
        lg.columnconfigure(0, weight=1)
        lg.rowconfigure(0, weight=1)
        self.log = tk.Text(lg, height=8, wrap="word", state="disabled", font="TkDefaultFont")
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lg, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)
        self.nb = nb

        # 進度
        p = ttk.LabelFrame(main, text="進度", padding=6)
        p.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        p.columnconfigure(0, weight=1)
        self.phase_lbl = ttk.Label(p, text="待命", font=("", 10, "bold"))
        self.phase_lbl.grid(row=0, column=0, sticky="w")
        self.pct_lbl = ttk.Label(p, text="", font=("", 10, "bold"))
        self.pct_lbl.grid(row=0, column=1, sticky="e")
        self.bar = ttk.Progressbar(p, mode="determinate", maximum=1, value=0)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        self.stat_lbl = ttk.Label(p, text="")
        self.stat_lbl.grid(row=2, column=0, columnspan=2, sticky="w")

        a = ttk.Frame(main)
        a.grid(row=5, column=0, sticky="e", pady=(8, 0))
        self.start_btn = ttk.Button(a, text="開始翻譯", command=self._start_translate)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(a, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

    # ------------------------------------------------------------ 設定存取

    def _load_settings(self):
        try:
            return core.load_json(SETTINGS_PATH)
        except (OSError, ValueError):
            return {}

    def _restore(self):
        s = self.s
        self.cat_var.set(s.get("category", "雲端 API"))
        self.src_var.set(s.get("src_lang", "ja"))
        self.tgt_var.set(s.get("tgt_lang", "zh-TW"))
        self.ctx_var.set(s.get("context_lines", 5))
        self.bg_text.insert("1.0", s.get("background", ""))
        self._on_category(s.get("preset"))
        if s.get("input") and os.path.exists(s["input"]):
            self._set_input(s["input"], s.get("output"))

    def _save_engine_fields(self):
        """把目前服務的欄位存到 per_preset，切換服務時不會弄丟金鑰。"""
        name = self.cur_preset
        if name:
            self.s.setdefault("per_preset", {})[name] = dict(
                base_url=self.url_var.get(), api_key=self.key_var.get(), model=self.model_var.get(),
                workers=self._int(self.workers_var, 1), batch=self._int(self.batch_var, 10))

    def _save_settings(self):
        self._save_engine_fields()
        self.s.update(category=self.cat_var.get(), preset=self.cur_preset,
                      src_lang=self.src_var.get(), tgt_lang=self.tgt_var.get(),
                      context_lines=self._int(self.ctx_var, 5), background=self.bg_text.get("1.0", "end").strip(),
                      input=self.in_var.get(), output=self.out_var.get())
        try:
            core.save_json(SETTINGS_PATH, self.s)
        except OSError as e:
            self._log(f"設定存檔失敗：{e}")

    @staticmethod
    def _int(var, default):
        try:
            return max(0, int(var.get()))
        except (tk.TclError, ValueError):
            return default

    # ------------------------------------------------------------ 引擎選擇

    def _on_category(self, preset=None):
        self._save_engine_fields()
        names = list(PRESETS[self.cat_var.get()])
        self.preset_cb["values"] = names
        self.preset_var.set(preset if preset in names else names[0])
        self._on_preset(save=False)

    def _preset(self):
        return PRESETS[self.cat_var.get()][self.preset_var.get()]

    def _on_preset(self, save=True):
        if save:
            self._save_engine_fields()
        p = self._preset()
        saved = self.s.get("per_preset", {}).get(self.preset_var.get(), {})
        self.url_var.set(saved.get("base_url") or p.get("base_url", ""))
        self.key_var.set(saved.get("api_key", ""))
        self.model_var.set(saved.get("model") or p.get("model", ""))
        self.workers_var.set(saved.get("workers", p["workers"]))
        self.batch_var.set(saved.get("batch", p["batch"]))
        kind = p["kind"]
        self.url_ent.configure(state="normal" if kind == "openai" else "disabled")
        self.key_ent.configure(state="disabled" if kind == "google" else "normal")
        self.key_ent.configure(show="" if kind == "mymemory" else "•")
        self.key_lbl.configure(text=p.get("key_label", "API Key"))
        llm = kind in ("openai", "claude")
        self.model_cb.configure(state="normal" if llm else "disabled")
        self.models_btn.configure(state="normal" if llm else "disabled")
        hints = {
            "claude": "金鑰留空會讀 ANTHROPIC_API_KEY 環境變數",
            "mymemory": "免費、不需註冊；無上下文，術語以替換方式套用",
            "google": "非官方端點，可能被暫時封鎖；無上下文",
        }
        if self.cat_var.get().startswith("本地"):
            hints["openai"] = "金鑰通常留空；請先啟動本地伺服器"
        self.hint.configure(text=hints.get(kind, ""))
        self.cur_preset = self.preset_var.get()

    def _make_engine(self):
        p = self._preset()
        return core.make_engine(p["kind"], self.url_var.get().strip(), self.key_var.get().strip(),
                                self.model_var.get().strip(), p.get("temperature"),
                                self.src_var.get(), self.tgt_var.get())

    def _engine_or_error(self):
        try:
            return self._make_engine()
        except core.FatalError as e:
            messagebox.showerror("引擎設定錯誤", str(e))

    def _fetch_models(self):
        eng = self._engine_or_error()
        if not eng:
            return

        def work():
            try:
                models = eng.list_models()
                self.q.put(("models", models))
            except Exception as e:
                self.q.put(("error", f"取得模型清單失敗：{e}"))
        threading.Thread(target=work, daemon=True).start()

    def _test_engine(self):
        eng = self._engine_or_error()
        if not eng:
            return

        def work():
            try:
                t0 = time.time()
                if eng.llm:
                    r = eng.complete("把使用者的文字翻譯成繁體中文，只輸出譯文。", "こんにちは、世界！")
                else:
                    r = eng.translate("こんにちは、世界！")
                self.q.put(("info", f"連線成功（{time.time() - t0:.1f} 秒）\n\nこんにちは、世界！ → {r.strip()[:200]}"))
            except Exception as e:
                self.q.put(("error", f"連線失敗：{e}"))
        self._log("測試連線中…")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 檔案

    def _pick_input(self):
        path = filedialog.askopenfilename(title="選擇 MTool 匯出的原文檔",
                                          filetypes=[("JSON", "*.json"), ("全部", "*.*")])
        if path:
            self._set_input(path)

    def _set_input(self, path, output=None):
        self.in_var.set(path)
        d, name = os.path.split(path)
        self.out_var.set(output or os.path.join(d, os.path.splitext(name)[0] + "_translated.json"))
        self.glossary = {}
        gp = self._glossary_path()
        if os.path.exists(gp):
            try:
                self.glossary = core.load_json(gp)
            except ValueError as e:
                messagebox.showerror("術語表讀取失敗", f"{gp}\n{e}")
        self._refresh_terms()

    def _pick_output(self):
        path = filedialog.asksaveasfilename(title="譯文輸出位置", defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            self.out_var.set(path)

    def _glossary_path(self):
        p = self.in_var.get()
        return os.path.splitext(p)[0] + ".glossary.json" if p else ""

    # ------------------------------------------------------------ 術語表

    def _refresh_terms(self):
        self.tree.delete(*self.tree.get_children())
        self.term_keys = sorted(self.glossary)
        for i, k in enumerate(self.term_keys):
            self.tree.insert("", "end", iid=str(i), values=(k, self.glossary[k]))
        self.term_count.configure(text=f"共 {len(self.glossary)} 個")

    def _save_terms(self):
        self._refresh_terms()
        gp = self._glossary_path()
        if gp:
            try:
                core.save_json(gp, self.glossary)
            except OSError as e:
                self._log(f"術語表存檔失敗：{e}")

    def _add_term(self):
        src = simpledialog.askstring("新增術語", "原文：", parent=self.root)
        if not src or not src.strip():
            return
        dst = simpledialog.askstring("新增術語", f"「{src.strip()}」的譯名：", parent=self.root,
                                     initialvalue=self.glossary.get(src.strip(), ""))
        if dst and dst.strip():
            self.glossary[src.strip()] = dst.strip()
            self._save_terms()

    def _edit_term(self):
        sel = self.tree.selection()
        if not sel:
            return
        k = self.term_keys[int(sel[0])]
        dst = simpledialog.askstring("編輯術語", f"「{k}」的譯名：", parent=self.root, initialvalue=self.glossary[k])
        if dst and dst.strip():
            self.glossary[k] = dst.strip()
            self._save_terms()

    def _del_terms(self):
        sel = self.tree.selection()
        if sel and messagebox.askyesno("刪除術語", f"刪除選取的 {len(sel)} 個術語？"):
            for i in sel:
                self.glossary.pop(self.term_keys[int(i)], None)
            self._save_terms()

    def _import_terms(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = core.load_json(path)
            n = 0
            for k, v in data.items():
                if isinstance(v, str) and k.strip() and v.strip():
                    self.glossary[k.strip()] = v.strip()
                    n += 1
            self._save_terms()
            self._log(f"匯入 {n} 個術語")
        except (OSError, ValueError, AttributeError) as e:
            messagebox.showerror("匯入失敗", str(e))

    def _export_terms(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            core.save_json(path, self.glossary)

    # ------------------------------------------------------------ 工作

    def _cfg(self):
        return dict(src_lang=self.src_var.get(), tgt_lang=self.tgt_var.get(),
                    batch_size=max(1, self._int(self.batch_var, 10)), workers=max(1, self._int(self.workers_var, 1)),
                    context_lines=self._int(self.ctx_var, 5), background=self.bg_text.get("1.0", "end").strip())

    def _run(self, kind):
        if self.busy:
            return
        src = self.in_var.get()
        if not os.path.exists(src):
            messagebox.showwarning("缺少原文檔", "請先選擇 MTool 匯出的原文 JSON（ManualTransFile.json）")
            return
        if kind == "terms" and not self._preset()["kind"] in ("openai", "claude"):
            messagebox.showinfo("需要 LLM", "自動擷取術語需要 LLM 引擎（雲端 API、本地 AI、本地 LLM）")
            return
        try:
            data = core.load_json(src)
            assert isinstance(data, dict)
        except Exception as e:
            messagebox.showerror("原文檔讀取失敗", f"不是有效的 MTool JSON：{e}")
            return
        engine = self._engine_or_error()
        if not engine:
            return
        self._save_settings()
        self.busy = True
        self.stop.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.nb.select(1)
        job = core.Job(engine, self._cfg(), dict(self.glossary),
                       lambda p: self.q.put(("progress", p)), lambda m: self.q.put(("log", m)), self.stop)
        out = self.out_var.get()

        def work():
            try:
                if kind == "terms":
                    self.q.put(("terms", job.extract_terms(data)))
                else:
                    job.translate(data, out)
                    self.q.put(("done", "已停止" if self.stop.is_set() else "完成"))
            except core.FatalError as e:
                self.q.put(("fatal", str(e)))
            except Exception as e:
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))
        self._log(f"＝＝ 開始{'擷取術語' if kind == 'terms' else '翻譯'}（{self.preset_var.get()}）＝＝")
        threading.Thread(target=work, daemon=True).start()

    def _start_translate(self):
        self._run("translate")

    def _extract(self):
        self._run("terms")

    def _stop(self):
        self.stop.set()
        self.stop_btn.configure(state="disabled")
        self.phase_lbl.configure(text="停止中…（等待進行中的請求結束並存檔）")

    def _finish(self):
        self.busy = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.root.title("MTool 翻譯器")

    # ------------------------------------------------------------ UI 更新（只在主執行緒）

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_progress(self, p):
        done, total = p["done"], p["total"]
        pct = 100.0 if total == 0 else done / total * 100
        self.bar.configure(maximum=max(total, 1), value=done if total else 1)
        unit = "段" if p["phase"] == "擷取術語" else "條"
        self.phase_lbl.configure(text=f"{p['phase']}　{done:,} / {total:,} {unit}")
        self.pct_lbl.configure(text=f"{pct:.1f}%")
        parts = []
        if p["phase"] == "翻譯":
            parts += [f"失敗 {p['failed']:,}", f"跳過 {p['skipped']:,}"]
        parts += [f"速度 {p['rate']:.2f} {unit}/秒" if p["rate"] else "速度 --",
                  f"已用 {fmt_time(p['elapsed'])}",
                  f"預估剩餘 {fmt_time(p['eta']) if done < total else fmt_time(0)}"]
        self.stat_lbl.configure(text="　｜　".join(parts))
        self.root.title(f"{pct:.1f}% {p['phase']} - MTool 翻譯器")

    def _poll(self):
        latest = None
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "progress":
                    latest = val  # 一次只畫最新的，避免大量更新卡住畫面
                    continue
                if latest:
                    self._show_progress(latest)
                    latest = None
                self._handle(kind, val)
        except queue.Empty:
            pass
        if latest:
            self._show_progress(latest)
        self.root.after(100, self._poll)

    def _handle(self, kind, val):
        if kind == "log":
            self._log(val)
        elif kind == "info":
            self._log(val.replace("\n\n", " "))
            messagebox.showinfo("測試連線", val)
        elif kind == "error":
            self._log(val)
            messagebox.showerror("錯誤", val)
        elif kind == "models":
            self.model_cb["values"] = val
            self._log(f"取得 {len(val)} 個模型")
        elif kind == "done":
            self._finish()
            self.phase_lbl.configure(text=f"{val}　" + self.phase_lbl.cget("text").split("　", 1)[-1])
            self._log(f"＝＝ {val}，已存檔：{self.out_var.get()} ＝＝")
            if val == "完成":
                messagebox.showinfo("翻譯完成", f"已輸出：\n{self.out_var.get()}\n\n"
                                    "在 MTool 的翻譯頁選「加載翻譯文件」載入這個檔案即可。")
        elif kind == "terms":
            self._finish()
            added = 0
            for k, v in val.items():
                if k not in self.glossary:
                    self.glossary[k] = v
                    added += 1
            self._save_terms()
            self.nb.select(0)
            msg = "已停止" if self.stop.is_set() else "完成"
            self.phase_lbl.configure(text=f"擷取術語{msg}")
            self._log(f"＝＝ 擷取術語{msg}：新增 {added} 個，請檢查後再開始翻譯 ＝＝")
        elif kind == "fatal":
            self._finish()
            self.phase_lbl.configure(text="已中止")
            self._log(f"＝＝ 中止：{val} ＝＝")
            messagebox.showerror("已中止", f"{val}\n\n已完成的部分都已存檔，修正後再按開始會從中斷處續翻。")

    def _on_close(self):
        if self.busy and not messagebox.askyesno("工作進行中", "翻譯還在進行，確定要關閉？（已完成的部分已存檔）"):
            return
        self.stop.set()
        self._save_settings()
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
