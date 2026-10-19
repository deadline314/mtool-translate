"""MTool 翻譯器介面：python app.py"""
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from settings import load_settings, save_settings
from translator import (CATEGORIES, LANGS, MTOOL_JSON, PHASE_TERMS, PHASE_TRANSLATE, PROVIDER_BY_ID, PROVIDERS,
                        ChatEngine, Job, JobConfig, LineEngine, TranslatorError, make_engine,
                        normalize_glossary, read_json, write_json_atomic)
from ui_widgets import cjk_wrap, show_guide, tip

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(HERE, "settings.json")
LOG_DIR = os.path.join(HERE, "logs")
APP_TITLE = "MTool 翻譯器"
SRC_LANGS = ["ja", "en", "zh-CN", "ko"]
TGT_LANGS = ["zh-TW", "zh-CN", "en", "ja"]
MAX_LOG_LINES = 2000          # 記錄區最多保留幾行，避免大量失敗訊息吃光記憶體、拖慢畫面
MAX_EVENTS_PER_TICK = 300     # 每次畫面更新最多處理幾則背景訊息
TEST_SENTENCE = "こんにちは、世界！"

log = logging.getLogger("app")


def fmt_time(sec):
    if sec is None:
        return "--:--:--"
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def lang_label(code):
    return f"{code}（{LANGS[code].split('（')[0]}）"


class App:
    def __init__(self, root, settings_path=SETTINGS_PATH):
        self.root = root
        self.settings_path = settings_path
        self.s = load_settings(settings_path)
        self.q = queue.Queue()
        self.job = None
        self.worker = None
        self.closing = False
        self.glossary = {}
        self.term_keys = []
        self.provider = PROVIDER_BY_ID[self.s["provider"]]
        self.tested_provider = None
        self.terms_extracted = False
        self.translated_once = False
        self.last_progress = None
        self.lockable = []   # (元件, 平常的 state)：執行中要鎖住的元件

        root.title(APP_TITLE)
        root.geometry("940x860")
        root.minsize(820, 720)
        root.report_callback_exception = self._on_tk_error
        self._build()
        self._restore()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)
        if not self.s["guide_seen"]:
            root.after(400, self._open_guide)

    # ================================================================ 版面

    def _lock(self, widget, normal="normal"):
        self.lockable.append((widget, normal))
        return widget

    def _build(self):
        pad = dict(padx=6, pady=3)
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # ---- 檔案
        f = ttk.LabelFrame(main, text="① 檔案", padding=6)
        f.grid(row=0, column=0, sticky="ew")
        f.columnconfigure(1, weight=1)
        self.in_var, self.out_var = tk.StringVar(), tk.StringVar()
        tip(ttk.Label(f, text="原文檔（MTool 匯出）"), "在 MTool 翻譯頁按「導出原文」產生的 ManualTransFile.json。"
            ).grid(row=0, column=0, sticky="w", **pad)
        tip(self._lock(ttk.Entry(f, textvariable=self.in_var, state="readonly"), "readonly"),
            "要翻譯的原文檔。請用右邊的「瀏覽…」選擇。").grid(row=0, column=1, sticky="ew", **pad)
        tip(self._lock(ttk.Button(f, text="瀏覽…", command=self._pick_input)),
            "選擇 MTool 匯出的原文檔。同資料夾的術語表會一起載入。").grid(row=0, column=2, **pad)
        tip(ttk.Label(f, text="譯文輸出"), "翻譯結果要存到哪裡。翻完後在 MTool 用「加載翻譯文件」載入這個檔案。"
            ).grid(row=1, column=0, sticky="w", **pad)
        tip(self._lock(ttk.Entry(f, textvariable=self.out_var)),
            "輸出檔已經存在時會從中斷處續翻：已翻好的不會重送，檔案裡其他條目也會保留。"
            ).grid(row=1, column=1, sticky="ew", **pad)
        tip(self._lock(ttk.Button(f, text="瀏覽…", command=self._pick_output)), "選擇譯文要存到哪裡。"
            ).grid(row=1, column=2, **pad)

        # ---- 引擎
        e = ttk.LabelFrame(main, text="② 翻譯引擎", padding=6)
        e.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        e.columnconfigure(1, weight=1)
        self.cat_var = tk.StringVar()
        cats = ttk.Frame(e)
        cats.grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        cat_help = {
            "雲端 API": "Claude、OpenAI、Gemini、DeepSeek 等。品質最好，支援上下文與術語表；需要金鑰，依用量計費。",
            "免費服務": "不需金鑰。沒有上下文能力，每天有額度上限，適合少量文本或先試試看。",
            "本地 AI": "日翻中專用的本地模型（Sakura），完全離線、不花錢，需要先在本機啟動 llama.cpp。",
            "本地 LLM": "Ollama、LM Studio 等本地通用模型，完全離線、不花錢，支援上下文與術語表；品質依模型而定。",
        }
        for c in CATEGORIES:
            tip(self._lock(ttk.Radiobutton(cats, text=c, value=c, variable=self.cat_var, command=self._on_category)),
                cat_help[c]).pack(side="left", padx=(0, 16))
        tip(ttk.Label(e, text="服務"), "這個類別底下可用的服務。").grid(row=1, column=0, sticky="w", **pad)
        self.provider_var = tk.StringVar()
        self.provider_cb = tip(self._lock(ttk.Combobox(e, textvariable=self.provider_var, state="readonly"), "readonly"),
                               lambda: self.provider.help)
        self.provider_cb.grid(row=1, column=1, sticky="ew", **pad)
        self.provider_cb.bind("<<ComboboxSelected>>", lambda _: self._on_provider_selected())
        self.help_lbl = ttk.Label(e, foreground="#555", wraplength=760, justify="left")
        self.help_lbl.grid(row=2, column=0, columnspan=3, sticky="w", padx=6)

        self.url_var, self.key_var, self.model_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.url_lbl = tip(ttk.Label(e, text="API 位址"), "服務的網址，填到 /v1 為止（本地服務通常是 http://localhost:埠號）。")
        self.url_lbl.grid(row=3, column=0, sticky="w", **pad)
        self.url_ent = tip(self._lock(ttk.Entry(e, textvariable=self.url_var)),
                           "服務的網址。選服務時會自動帶入預設值，通常不用改。")
        self.url_ent.grid(row=3, column=1, columnspan=2, sticky="ew", **pad)
        self.key_lbl = tip(ttk.Label(e, text="API Key"), lambda: self._key_help())
        self.key_lbl.grid(row=4, column=0, sticky="w", **pad)
        self.key_ent = tip(self._lock(ttk.Entry(e, textvariable=self.key_var, show="•")), lambda: self._key_help())
        self.key_ent.grid(row=4, column=1, columnspan=2, sticky="ew", **pad)
        self.model_lbl = tip(ttk.Label(e, text="模型"), "要使用的模型名稱。不確定時按「取得模型清單」。")
        self.model_lbl.grid(row=5, column=0, sticky="w", **pad)
        self.model_cb = tip(self._lock(ttk.Combobox(e, textvariable=self.model_var)),
                            "可以直接輸入，或按右邊「取得模型清單」後從下拉選單選。")
        self.model_cb.grid(row=5, column=1, sticky="ew", **pad)
        btns = ttk.Frame(e)
        btns.grid(row=5, column=2, sticky="w")
        self.models_btn = tip(self._lock(ttk.Button(btns, text="取得模型清單", command=self._fetch_models)),
                              lambda: "向服務查詢可用的模型，結果放進「模型」下拉選單。" if "model" in self.provider.fields
                              else "這個服務不需要選模型，所以停用。")
        self.models_btn.pack(side="left", padx=4)
        self.test_btn = tip(self._lock(ttk.Button(btns, text="測試連線", command=self._test_engine)),
                            f"用目前的設定實際翻譯一句「{TEST_SENTENCE}」，確認金鑰、位址、模型都正確。")
        self.test_btn.pack(side="left", padx=4)

        nums = ttk.Frame(e)
        nums.grid(row=6, column=0, columnspan=3, sticky="w", **pad)
        self.workers_var, self.batch_var = tk.IntVar(value=1), tk.IntVar(value=10)
        tip(ttk.Label(nums, text="同時請求數"), "同時送出幾個請求。雲端 API 可設 3～8 加快速度；本地模型建議 1。"
                                            "遇到「請求太頻繁」就調低。").pack(side="left")
        tip(self._lock(ttk.Spinbox(nums, from_=1, to=32, width=5, textvariable=self.workers_var)),
            "範圍 1～32。").pack(side="left", padx=(4, 18))
        tip(ttk.Label(nums, text="每批條數"), "一次請求翻幾句。多一點比較快、上下文較完整；"
                                          "模型常漏句或出錯時調低。本地模型建議 10 以內。").pack(side="left")
        tip(self._lock(ttk.Spinbox(nums, from_=1, to=100, width=5, textvariable=self.batch_var)),
            "範圍 1～100。一批的總字數也有上限，太長的句子會自動分開送。").pack(side="left", padx=4)

        # ---- 翻譯設定
        t = ttk.LabelFrame(main, text="③ 翻譯設定", padding=6)
        t.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        t.columnconfigure(1, weight=1)
        row = ttk.Frame(t)
        row.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.src_var, self.tgt_var, self.ctx_var = tk.StringVar(), tk.StringVar(), tk.IntVar(value=5)
        tip(ttk.Label(row, text="來源"), "遊戲原文的語言。也用來判斷哪些條目需要翻譯（純數字、符號會跳過）。"
            ).pack(side="left")
        tip(self._lock(ttk.Combobox(row, textvariable=self.src_var, values=[lang_label(c) for c in SRC_LANGS],
                                    width=16, state="readonly"), "readonly"), "遊戲原文的語言。").pack(
            side="left", padx=(4, 16))
        tip(ttk.Label(row, text="目標"), "要翻成哪種語言。").pack(side="left")
        tip(self._lock(ttk.Combobox(row, textvariable=self.tgt_var, values=[lang_label(c) for c in TGT_LANGS],
                                    width=16, state="readonly"), "readonly"),
            "要翻成哪種語言。Sakura 只會輸出簡體中文。").pack(side="left", padx=(4, 16))
        tip(ttk.Label(row, text="前文參考行數"), "翻譯每批時附上前面幾句的原文與譯文，讓模型知道語境和說話者。"
                                             "0 表示不附；只有雲端 API 與本地 LLM 會用到。").pack(side="left")
        tip(self._lock(ttk.Spinbox(row, from_=0, to=30, width=5, textvariable=self.ctx_var)),
            "範圍 0～30，建議 3～8。越多越連貫，但每次請求的字數也越多。").pack(side="left", padx=4)
        tip(ttk.Label(t, text="遊戲背景"), "寫下遊戲類型、角色設定、說話語氣，會附在每次請求裡。"
            ).grid(row=1, column=0, sticky="nw", **pad)
        self.bg_text = tk.Text(t, height=3, wrap="char", font="TkDefaultFont", undo=True)
        tip(self._lock(self.bg_text), "例：奇幻 RPG。主角アリス是女騎士，說話直率；魔王ゼノ說話古風。"
                                      "只有雲端 API 與本地 LLM 會用到。")
        self.bg_text.grid(row=1, column=1, sticky="ew", **pad)

        # ---- 術語表 / 記錄
        nb = ttk.Notebook(main)
        nb.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        main.rowconfigure(3, weight=1)
        self.nb = nb

        g = ttk.Frame(nb, padding=4)
        nb.add(g, text="術語表")
        g.columnconfigure(0, weight=1)
        g.rowconfigure(1, weight=1)
        gb = ttk.Frame(g)  # 按鈕排成一列放在上方，視窗矮的時候也不會被切掉
        gb.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        for spec in [
            ("新增", self._add_term, "新增一個術語：先輸入原文，再輸入譯名。原文已存在時會更新譯名。"),
            ("編輯", self._edit_term, "修改選取術語的譯名（也可以直接雙擊）。"),
            ("刪除", self._del_terms, "刪除選取的術語（可多選，也可以按 Delete 鍵）。"),
            None,
            ("自動擷取術語", self._extract, lambda: self._extract_help()),
            None,
            ("匯入…", self._import_terms, "從 JSON 檔匯入術語（格式：{\"原文\": \"譯名\"}），同名的會被覆蓋。"),
            ("匯出…", self._export_terms, "把術語表存成 JSON 檔，可以給其他遊戲或其他人使用。"),
        ]:
            if spec is None:
                ttk.Separator(gb, orient="vertical").pack(side="left", fill="y", padx=6)
                continue
            text, cmd, help_text = spec
            btn = tip(self._lock(ttk.Button(gb, text=text, command=cmd)), help_text)
            btn.pack(side="left", padx=2)
            if cmd == self._extract:
                self.extract_btn = btn
        self.term_count = ttk.Label(gb, foreground="#666")
        self.term_count.pack(side="right", padx=4)
        self.tree = ttk.Treeview(g, columns=("src", "dst"), show="headings", selectmode="extended", height=5)
        self.tree.heading("src", text="原文")
        self.tree.heading("dst", text="譯名")
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _: self._edit_term())
        self.tree.bind("<Delete>", lambda _: self._del_terms())
        tip(self.tree, "翻譯時，句子裡出現的術語一律使用這裡的譯名。雙擊可修改譯名，選取後按 Delete 可刪除。")
        sb = ttk.Scrollbar(g, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        lg = ttk.Frame(nb, padding=4)
        nb.add(lg, text="記錄")
        lg.columnconfigure(0, weight=1)
        lg.rowconfigure(0, weight=1)
        self.log = tk.Text(lg, height=6, wrap="char", state="disabled", font="TkDefaultFont")
        self.log.grid(row=0, column=0, sticky="nsew")
        tip(self.log, f"執行過程與每一條失敗的原因。只保留最近 {MAX_LOG_LINES} 行；完整記錄在 logs 資料夾。")
        lsb = ttk.Scrollbar(lg, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)

        # ---- 進度
        p = ttk.LabelFrame(main, text="④ 進度", padding=6)
        p.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        p.columnconfigure(0, weight=1)
        self.phase_lbl = tip(ttk.Label(p, text="待命", font=("", 10, "bold")),
                             "目前階段，以及已處理數／需要處理的總數（失敗的也算已處理）。")
        self.phase_lbl.grid(row=0, column=0, sticky="w")
        self.pct_lbl = tip(ttk.Label(p, text="", font=("", 10, "bold")), "完成百分比。")
        self.pct_lbl.grid(row=0, column=1, sticky="e")
        self.bar = tip(ttk.Progressbar(p, mode="determinate", maximum=1, value=0),
                       "依實際處理完的條數前進；續翻時從已完成的數量開始。")
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        self.stat_lbl = tip(ttk.Label(p, text=""),
                            "失敗：保留原文的條數（原因見記錄）。跳過：數字、符號等不需翻譯的條數。\n"
                            "速度與預估剩餘時間只用這次新翻的條目計算。")
        self.stat_lbl.grid(row=2, column=0, columnspan=2, sticky="w")

        # ---- 下方：下一步提示 + 按鈕
        a = ttk.Frame(main)
        a.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        a.columnconfigure(0, weight=1)
        self.next_lbl = tip(ttk.Label(a, foreground="#1a5fb4"), "依目前狀態建議的下一步。")
        self.next_lbl.grid(row=0, column=0, sticky="w")
        tip(ttk.Button(a, text="使用說明", command=self._open_guide), "完整的操作步驟與小技巧。").grid(
            row=0, column=1, padx=4)
        self.start_btn = tip(self._lock(ttk.Button(a, text="開始翻譯", command=self._start_translate)),
                             "開始翻譯。輸出檔已存在時會從中斷處續翻。")
        self.start_btn.grid(row=0, column=2, padx=4)
        self.stop_btn = tip(ttk.Button(a, text="停止", command=self._stop, state="disabled"),
                            "不再送出新請求；進行中的請求結束後存檔停止。之後按「開始翻譯」會接著翻。")
        self.stop_btn.grid(row=0, column=3, padx=4)

    # ================================================================ 說明文字

    def _key_help(self):
        p = self.provider
        if p.kind == "mymemory":
            return "選填。填 Email 可以把每日免費額度從約 5,000 字提高到約 50,000 字。"
        if p.kind == "claude":
            return "Anthropic Console 申請的 API Key（sk-ant- 開頭）。留空會讀環境變數 ANTHROPIC_API_KEY。只存在本機。"
        if p.category.startswith("本地"):
            return "本地服務通常不需要金鑰，留空即可。"
        return "服務商提供的 API Key。只存在本機的 settings.json。"

    def _extract_help(self):
        if not self.provider.supports_terms_extraction:
            return "自動擷取術語需要對話型 LLM（雲端 API 或本地 LLM）。目前的引擎不支援，可以手動新增或匯入術語。"
        return ("讓 AI 掃過全文，找出角色名、地名、物品、技能等專有名詞並給出譯名，加入術語表。"
                "原文沒出現的詞會被濾掉。建議翻譯前做一次，檢查譯名後再開始翻譯。會產生額外的 API 用量。")

    # ================================================================ 設定

    def _restore(self):
        s = self.s
        self.src_var.set(lang_label(s["src_lang"]))
        self.tgt_var.set(lang_label(s["tgt_lang"]))
        self.ctx_var.set(s["context_lines"])
        self.bg_text.insert("1.0", s["background"])
        self.cat_var.set(self.provider.category)
        self._fill_provider_list()
        self._load_provider_fields()
        if s["input"] and os.path.exists(s["input"]):
            self._set_input(s["input"], s["output"] or None)
        self._update_next_step()

    def _store_provider_fields(self):
        """目前服務的欄位記到 per_provider，切換服務時不會弄丟金鑰。"""
        self.s["per_provider"][self.provider.id] = dict(
            base_url=self.url_var.get().strip(), api_key=self.key_var.get().strip(),
            model=self.model_var.get().strip(), workers=self._int(self.workers_var, 1, 1, 32),
            batch=self._int(self.batch_var, 10, 1, 100))

    def _save_settings(self):
        self._store_provider_fields()
        self.s.update(provider=self.provider.id, src_lang=self._lang(self.src_var), tgt_lang=self._lang(self.tgt_var),
                      context_lines=self._int(self.ctx_var, 5, 0, 30), background=self.bg_text.get("1.0", "end").strip(),
                      input=self.in_var.get(), output=self.out_var.get().strip())
        try:
            save_settings(self.settings_path, self.s)
        except OSError as e:
            self._log(f"設定存檔失敗：{e}")

    @staticmethod
    def _int(var, default, lo, hi):
        try:
            return min(hi, max(lo, int(var.get())))
        except (tk.TclError, ValueError):
            return default

    @staticmethod
    def _lang(var):
        return var.get().split("（")[0]

    # ================================================================ 引擎選擇

    def _fill_provider_list(self):
        names = [p.name for p in PROVIDERS if p.category == self.cat_var.get()]
        self.provider_cb["values"] = names
        self.provider_var.set(self.provider.name)

    def _on_category(self):
        self._store_provider_fields()
        first = next(p for p in PROVIDERS if p.category == self.cat_var.get())
        self.provider = first
        self._fill_provider_list()
        self._load_provider_fields()

    def _on_provider_selected(self):
        self._store_provider_fields()
        self.provider = next(p for p in PROVIDERS if p.name == self.provider_var.get())
        self._load_provider_fields()

    def _load_provider_fields(self):
        p = self.provider
        saved = self.s["per_provider"].get(p.id, {})
        self.url_var.set(saved.get("base_url") or p.base_url)
        self.key_var.set(saved.get("api_key", ""))
        self.model_var.set(saved.get("model") or p.model)
        self.model_cb["values"] = ()
        self.workers_var.set(saved.get("workers", p.workers))
        self.batch_var.set(saved.get("batch", p.batch))
        self.help_lbl.configure(text=cjk_wrap(p.help))
        self.key_lbl.configure(text=p.key_label)
        self.key_ent.configure(show="" if p.kind == "mymemory" else "•")
        self._apply_field_states()
        self._update_next_step()

    def _apply_field_states(self):
        """依服務需要的欄位啟用／停用；執行中全部鎖住。"""
        p, busy = self.provider, self.job is not None
        for w, normal in self.lockable:
            w.configure(state="disabled" if busy else normal)
        self.bg_text.configure(background="#f0f0f0" if busy else "white")  # Text 停用時外觀不會自己變灰
        if busy:
            return
        self.url_ent.configure(state="normal" if "url" in p.fields else "disabled")
        self.key_ent.configure(state="normal" if "key" in p.fields else "disabled")
        self.model_cb.configure(state="normal" if "model" in p.fields else "disabled")
        self.models_btn.configure(state="normal" if "model" in p.fields else "disabled")
        self.extract_btn.configure(state="normal" if p.supports_terms_extraction else "disabled")

    def _make_engine(self):
        return make_engine(self.provider, self.url_var.get(), self.key_var.get(), self.model_var.get(),
                           self._lang(self.src_var), self._lang(self.tgt_var))

    def _engine_or_error(self):
        try:
            return self._make_engine()
        except TranslatorError as e:
            messagebox.showerror("引擎設定錯誤", str(e), parent=self.root)

    def _run_bg(self, fn, done_kind):
        """在背景執行（不卡畫面），結果丟回主執行緒處理。背景執行緒只碰佇列，不碰任何介面物件。"""
        q = self.q

        def work():
            try:
                q.put((done_kind, fn()))
            except TranslatorError as e:
                q.put(("bg_error", (done_kind, str(e))))
            except Exception as e:
                log.exception("background task failed")
                q.put(("bg_error", (done_kind, f"{type(e).__name__}: {e}")))
        threading.Thread(target=work, daemon=True).start()

    def _fetch_models(self):
        eng = self._engine_or_error()
        if eng:
            self.models_btn.configure(state="disabled")
            self._log("取得模型清單中…")
            self._run_bg(eng.list_models, "models")

    def _test_engine(self):
        eng = self._engine_or_error()
        if not eng:
            return
        tgt = LANGS[self._lang(self.tgt_var)]
        provider_id = self.provider.id

        def test():
            t0 = time.monotonic()
            if isinstance(eng, ChatEngine):
                r = eng.complete(f"把使用者的文字翻譯成{tgt}，只輸出譯文。", TEST_SENTENCE)
            elif isinstance(eng, LineEngine):
                r = eng.translate_lines([TEST_SENTENCE], {})[0]
            else:
                r = eng.translate(TEST_SENTENCE)
            return provider_id, time.monotonic() - t0, r.strip()[:200]
        self.test_btn.configure(state="disabled")
        self._log("測試連線中…")
        self._run_bg(test, "tested")

    # ================================================================ 檔案

    def _pick_input(self):
        path = filedialog.askopenfilename(parent=self.root, title="選擇 MTool 匯出的原文檔",
                                          filetypes=[("MTool 原文檔", "*.json"), ("全部檔案", "*.*")])
        if path:
            self._set_input(path)

    def _set_input(self, path, output=None):
        self.in_var.set(path)
        root, _ = os.path.splitext(path)
        self.out_var.set(output or root + "_translated.json")
        self.terms_extracted = False
        self.translated_once = False
        self._load_glossary()
        self._update_next_step()

    def _pick_output(self):
        cur = self.out_var.get()
        path = filedialog.asksaveasfilename(parent=self.root, title="譯文輸出位置", defaultextension=".json",
                                            initialdir=os.path.dirname(cur) or None,
                                            initialfile=os.path.basename(cur) or None,
                                            filetypes=[("JSON", "*.json")])
        if path:
            self.out_var.set(path)

    def _glossary_path(self):
        p = self.in_var.get()
        return os.path.splitext(p)[0] + ".glossary.json" if p else ""

    # ================================================================ 術語表

    def _load_glossary(self):
        self.glossary = {}
        gp = self._glossary_path()
        if gp and os.path.exists(gp):
            try:
                self.glossary = normalize_glossary(read_json(gp))
            except (OSError, ValueError) as e:
                messagebox.showerror("術語表讀取失敗", f"{gp}\n{e}\n\n先以空的術語表開始；原檔案不會被覆蓋，"
                                                 "除非你修改了術語表。", parent=self.root)
        self._refresh_terms()

    def _refresh_terms(self):
        self.tree.delete(*self.tree.get_children())
        self.term_keys = sorted(self.glossary)
        for i, k in enumerate(self.term_keys):
            self.tree.insert("", "end", iid=str(i), values=(k, self.glossary[k]))
        self.term_count.configure(text=f"共 {len(self.glossary)} 個")

    def _save_terms(self, path=None):
        self._refresh_terms()
        gp = path or self._glossary_path()
        if not gp:
            return
        try:
            write_json_atomic(gp, self.glossary)
        except OSError as e:
            self._log(f"術語表存檔失敗：{e}")
            messagebox.showerror("術語表存檔失敗", str(e), parent=self.root)

    def _need_input(self):
        if not self.in_var.get():
            messagebox.showinfo("先選原文檔", "術語表跟著原文檔存放，請先選擇原文檔。", parent=self.root)
            return False
        return True

    def _add_term(self):
        if not self._need_input():
            return
        src = simpledialog.askstring("新增術語", "原文：", parent=self.root)
        if not src or not src.strip():
            return
        src = src.strip()
        dst = simpledialog.askstring("新增術語", f"「{src}」的譯名：", parent=self.root,
                                     initialvalue=self.glossary.get(src, ""))
        if dst and dst.strip():
            self.glossary[src] = dst.strip()
            self._save_terms()

    def _selected_terms(self):
        return [self.term_keys[int(i)] for i in self.tree.selection() if int(i) < len(self.term_keys)]

    def _edit_term(self):
        if self.job:
            return
        sel = self._selected_terms()
        if not sel:
            messagebox.showinfo("編輯術語", "請先在術語表選一個術語。", parent=self.root)
            return
        k = sel[0]
        dst = simpledialog.askstring("編輯術語", f"「{k}」的譯名：", parent=self.root, initialvalue=self.glossary[k])
        if dst and dst.strip():
            self.glossary[k] = dst.strip()
            self._save_terms()

    def _del_terms(self):
        if self.job:
            return
        sel = self._selected_terms()
        if sel and messagebox.askyesno("刪除術語", f"刪除選取的 {len(sel)} 個術語？", parent=self.root):
            for k in sel:
                self.glossary.pop(k, None)
            self._save_terms()

    def _import_terms(self):
        if not self._need_input():
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            data = normalize_glossary(read_json(path))
        except (OSError, ValueError) as e:
            messagebox.showerror("匯入失敗", str(e), parent=self.root)
            return
        self.glossary.update(data)
        self._save_terms()
        self._log(f"匯入 {len(data)} 個術語")

    def _export_terms(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            write_json_atomic(path, self.glossary)
            self._log(f"已匯出 {len(self.glossary)} 個術語：{path}")
        except OSError as e:
            messagebox.showerror("匯出失敗", str(e), parent=self.root)

    # ================================================================ 工作

    def _check_files(self):
        src, out = self.in_var.get(), self.out_var.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showwarning("缺少原文檔", "請先選擇 MTool 匯出的原文檔（ManualTransFile.json）。", parent=self.root)
            return None
        if not out:
            messagebox.showwarning("缺少輸出位置", "請填寫譯文要存到哪裡。", parent=self.root)
            return None
        out_dir = os.path.dirname(os.path.abspath(out))
        if not os.path.isdir(out_dir):
            messagebox.showwarning("資料夾不存在", f"輸出資料夾不存在：\n{out_dir}", parent=self.root)
            return None
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(out)) and \
                not messagebox.askyesno("會覆蓋原文檔", "輸出檔和原文檔是同一個檔案，翻譯結果會直接寫進 MTool 匯出的原文檔。"
                                                   "\n\n確定要這樣做嗎？", icon="warning", parent=self.root):
            return None
        try:
            entries = MTOOL_JSON.load_source(src)
        except TranslatorError as e:
            messagebox.showerror("原文檔讀取失敗", str(e), parent=self.root)
            return None
        return entries, out

    def _start_job(self, kind):
        if self.job:
            return
        checked = self._check_files()
        if not checked:
            return
        entries, out = checked
        engine = self._engine_or_error()
        if not engine:
            return
        try:
            cfg = JobConfig(src_lang=self._lang(self.src_var), tgt_lang=self._lang(self.tgt_var),
                            batch_size=self._int(self.batch_var, 10, 1, 100),
                            workers=self._int(self.workers_var, 1, 1, 32),
                            context_lines=self._int(self.ctx_var, 5, 0, 30),
                            background=self.bg_text.get("1.0", "end").strip())
        except TranslatorError as e:
            messagebox.showerror("設定錯誤", str(e), parent=self.root)
            return
        self._save_settings()
        q = self.q  # 背景執行緒只碰佇列，不碰任何介面物件
        job = Job(engine, cfg, self.glossary, lambda p: q.put(("progress", p)), lambda m: q.put(("log", m)))
        self.job, self.last_progress = job, None
        self._apply_field_states()
        self._update_next_step()
        self.stop_btn.configure(state="normal")
        self.nb.select(1)
        gp = self._glossary_path()
        title = PHASE_TERMS if kind == "terms" else PHASE_TRANSLATE
        self._log(f"＝＝ 開始{title}（{self.provider.name}）＝＝")
        self.phase_lbl.configure(text=f"{title}準備中…")

        def work():
            try:
                if kind == "terms":
                    q.put(("terms_done", (job.extract_terms(entries), gp, job.user_stopped)))
                else:
                    q.put(("done", job.translate(entries, out)))
            except TranslatorError as e:
                q.put(("fatal", (kind, str(e), dict(job.found_terms), gp)))
            except Exception as e:
                log.exception("job crashed")
                q.put(("fatal", (kind, f"程式錯誤：{type(e).__name__}: {e}", dict(job.found_terms), gp)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _start_translate(self):
        self._start_job("translate")

    def _extract(self):
        if self._need_input():
            self._start_job("terms")

    def _stop(self):
        if self.job:
            self.job.stop()
            self.stop_btn.configure(state="disabled")
            self.phase_lbl.configure(text="停止中…（等進行中的請求結束後存檔）")

    def _job_finished(self):
        self.job = None
        self.stop_btn.configure(state="disabled")
        self._apply_field_states()
        self.root.title(APP_TITLE)
        self._update_next_step()

    # ================================================================ 畫面更新（只在主執行緒）

    def _log(self, msg):
        log.info(msg)
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_progress(self, p):
        self.last_progress = p
        unit = "段" if p.phase == PHASE_TERMS else "條"
        self.bar.configure(maximum=max(p.total, 1), value=p.done if p.total else 1)
        prefix = "停止中…　" if self.job and self.job.user_stopped else ""
        self.phase_lbl.configure(text=f"{prefix}{p.phase}　{p.done:,} / {p.total:,} {unit}")
        self.pct_lbl.configure(text=f"{p.percent:.1f}%")
        parts = []
        if p.phase == PHASE_TRANSLATE:
            parts += [f"失敗 {p.failed:,}", f"跳過 {p.skipped:,}"]
        parts += [f"速度 {p.rate:,.2f} {unit}/秒" if p.rate else "速度 --",
                  f"已用 {fmt_time(p.elapsed)}",
                  f"預估剩餘 {fmt_time(p.eta) if p.done < p.total else fmt_time(0)}"]
        self.stat_lbl.configure(text="　｜　".join(parts))
        self.root.title(f"{p.percent:.1f}% {p.phase} - {APP_TITLE}")

    def _final_phase_label(self, status):
        p = self.last_progress
        if p:
            unit = "段" if p.phase == PHASE_TERMS else "條"
            self.phase_lbl.configure(text=f"{p.phase}{status}　{p.done:,} / {p.total:,} {unit}")
        else:
            self.phase_lbl.configure(text=status)

    def _update_next_step(self):
        if self.job:
            text = "執行中…可以隨時按「停止」，已完成的部分會保留。"
        elif not self.in_var.get():
            text = "下一步：① 按「瀏覽…」選擇 MTool 匯出的原文檔（ManualTransFile.json）"
        elif self.translated_once:
            text = "下一步：到 MTool 翻譯頁按「加載翻譯文件」，選擇輸出檔"
        elif self.tested_provider != self.provider.id:
            text = "下一步：② 選好翻譯引擎後按「測試連線」，確認可以使用"
        elif self.provider.supports_terms_extraction and not self.glossary and not self.terms_extracted:
            text = "下一步：（建議）按「自動擷取術語」建立術語表，檢查譯名後再翻譯"
        else:
            text = "下一步：按「開始翻譯」"
        self.next_lbl.configure(text=text)

    def _poll(self):
        try:
            latest = None
            for _ in range(MAX_EVENTS_PER_TICK):
                try:
                    kind, val = self.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    latest = val  # 一次只畫最新的，避免大量更新卡住畫面
                    continue
                if latest:
                    self._show_progress(latest)
                    latest = None
                self._handle(kind, val)
            if latest:
                self._show_progress(latest)
        except Exception:
            log.exception("UI update failed")
        finally:
            self.root.after(100, self._poll)

    def _handle(self, kind, val):
        if kind == "log":
            self._log(val)
        elif kind == "models":
            self.model_cb["values"] = val
            self._apply_field_states()
            self._log(f"取得 {len(val)} 個模型" + ("，請從「模型」下拉選單選擇" if val else ""))
        elif kind == "tested":
            provider_id, secs, text = val
            self.tested_provider = provider_id
            self._apply_field_states()
            msg = f"連線成功（{secs:.1f} 秒）：{TEST_SENTENCE} → {text}"
            self._log(msg)
            self._update_next_step()
            messagebox.showinfo("測試連線", msg, parent=self.root)
        elif kind == "bg_error":
            what, msg = val
            self._apply_field_states()
            title = "取得模型清單失敗" if what == "models" else "連線失敗"
            self._log(f"{title}：{msg}")
            messagebox.showerror(title, msg, parent=self.root)
        elif kind == "done":
            self._on_translate_done(val)
        elif kind == "terms_done":
            found, gp, stopped = val
            self._merge_terms(found, gp)
            self._job_finished()
            status = "已停止" if stopped else "完成"
            self._final_phase_label(status)
            self.nb.select(0)
            self._log(f"＝＝ 擷取術語{status}：新增 {len(found)} 個，請檢查譯名後再開始翻譯 ＝＝")
        elif kind == "fatal":
            self._on_fatal(*val)

    def _merge_terms(self, found, gp):
        added = {k: v for k, v in found.items() if k not in self.glossary}
        if added and gp == self._glossary_path():
            self.glossary.update(added)
            self._save_terms(gp)
        self.terms_extracted = True
        return added

    def _on_translate_done(self, summary):
        self._job_finished()
        stopped = summary["stopped"]
        self._final_phase_label("已停止" if stopped else "完成")
        saved = summary["saved_to"]
        self._log(f"＝＝ {'已停止' if stopped else '完成'}：處理 {summary['done']:,} / {summary['total']:,} 條，"
                  f"失敗 {summary['failed']:,} 條，已存檔：{saved} ＝＝")
        if self.closing:
            return
        if saved != self.out_var.get().strip():
            messagebox.showwarning("輸出檔無法寫入", f"原本的輸出檔無法寫入（可能被其他程式開著），結果已另存到：\n{saved}",
                                   parent=self.root)
        if not stopped:
            self.translated_once = True
            self._update_next_step()
            extra = f"\n\n其中 {summary['failed']:,} 條翻譯失敗、保留原文，原因見「記錄」分頁；再按一次開始翻譯會重試這些句子。" \
                if summary["failed"] else ""
            messagebox.showinfo("翻譯完成", f"已輸出：\n{saved}\n\n到 MTool 翻譯頁按「加載翻譯文件」載入這個檔案即可。{extra}",
                                parent=self.root)

    def _on_fatal(self, kind, msg, partial_terms, gp):
        self._job_finished()
        self._final_phase_label("已中止")
        self._log(f"＝＝ 中止：{msg} ＝＝")
        if kind == "terms":
            added = self._merge_terms(partial_terms, gp)
            hint = f"中止前找到的 {len(added)} 個術語已加入術語表。" if added else ""
        else:
            hint = "已完成的部分都已存檔，修正問題後再按「開始翻譯」會從中斷處續翻。"
        if not self.closing:
            messagebox.showerror("已中止", f"{msg}\n\n{hint}", parent=self.root)

    def _open_guide(self):
        def seen():
            self.s["guide_seen"] = True
            self._save_settings()
        show_guide(self.root, seen)

    def _on_tk_error(self, exc, val, tb):
        log.error("UI callback error", exc_info=(exc, val, tb))
        messagebox.showerror("程式錯誤", f"{exc.__name__}: {val}\n\n詳細記錄在 logs 資料夾。", parent=self.root)

    def _on_close(self):
        if self.job:
            if not messagebox.askyesno("工作進行中", "翻譯還在進行。要停止並存檔後關閉嗎？", parent=self.root):
                return
            self.closing = True
            self.job.stop()
            self.phase_lbl.configure(text="停止中，存檔後關閉…")
            self._save_settings()
            self._wait_then_close(time.monotonic())
            return
        self._save_settings()
        self.root.destroy()

    def _wait_then_close(self, t0):
        # ponytail: 最多等 60 秒；卡在超長的網路請求時直接關閉（最多損失最後幾秒的結果）
        if self.worker and self.worker.is_alive() and time.monotonic() - t0 < 60:
            self.root.after(200, self._wait_then_close, t0)
        else:
            self.root.destroy()


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "mtool-translate.log"),
                                                   maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    threading.excepthook = lambda a: log.error("thread error", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def main():
    setup_logging()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
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
