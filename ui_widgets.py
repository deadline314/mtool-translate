"""介面小元件：滑鼠停留說明（Tooltip）與使用說明視窗。"""
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

GUIDE_TEXT = """MTool 翻譯器使用說明

1. 在 MTool 匯出原文
   用 MTool 開啟遊戲 → 翻譯頁 →「導出原文」，會得到 ManualTransFile.json。

2. 選擇原文檔
   按「原文檔」旁的「瀏覽…」選擇剛匯出的檔案。
   譯文預設存到同資料夾的「*_translated.json」，術語表存在「*.glossary.json」。

3. 選擇翻譯引擎並測試連線
   ・雲端 API：品質最好，需要金鑰，依用量計費
   ・免費服務：不需金鑰，但沒有上下文能力、每天有額度
   ・本地 AI：日翻中專用模型（Sakura），完全離線
   ・本地 LLM：Ollama、LM Studio，完全離線
   填好欄位後按「測試連線」，看到譯文就代表可以用。

4.（建議）自動擷取術語
   按術語表分頁的「自動擷取術語」，讓 AI 先掃過全文，找出角色名、地名、物品等專有名詞。
   檢查並修改譯名後再開始翻譯，全篇名詞就會一致。也可以手動新增、匯入術語。

5. 開始翻譯
   進度列會顯示已處理數量、失敗、速度與預估剩餘時間。
   隨時可以按「停止」；已完成的部分會自動存檔，下次按「開始翻譯」會從中斷處接著翻。

6. 載入 MTool
   回到 MTool 翻譯頁 →「加載翻譯文件」→ 選擇輸出檔，遊戲裡就會顯示譯文。

小技巧
・把滑鼠移到任何欄位或按鈕上，可以看到詳細說明。
・遊戲控制碼（顏色、變數、角色名等）會自動保護，翻譯後原樣保留。
・翻譯失敗的句子會保留原文，原因列在「記錄」分頁；之後再按開始會重試這些句子。
・在「遊戲背景」寫下角色設定與語氣，LLM 的翻譯會更貼近原作。
・API 金鑰只存在本機的 settings.json，不會上傳到別處。
"""


def cjk_wrap(text):
    """Tk 的自動換行只在半形空白處斷開；中英混排時改成不斷行空白，讓中文能逐字換行、不會整段跳到下一行。"""
    return (text or "").replace(" ", "\u00a0")


class Tooltip:
    """滑鼠停留一下就顯示說明；text 可以是字串或回傳字串的函式（內容會隨狀態變化）。"""

    def __init__(self, widget, text, delay=450, wraplength=380):
        self.widget, self.text, self.delay, self.wraplength = widget, text, delay, wraplength
        self._after = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        self._after = None
        text = self.text() if callable(self.text) else self.text
        if not text or self._tip:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=cjk_wrap(text), justify="left", wraplength=self.wraplength, background="#fffbe6",
                 foreground="#333", relief="solid", borderwidth=1, padx=8, pady=5,
                 font="TkDefaultFont").pack()
        tip.update_idletasks()
        x = self.widget.winfo_pointerx() + 14
        y = self.widget.winfo_pointery() + 20
        sw, sh = tip.winfo_screenwidth(), tip.winfo_screenheight()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        x = min(x, sw - w - 8)
        if y + h > sh - 8:
            y = self.widget.winfo_pointery() - h - 10
        tip.wm_geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


def tip(widget, text):
    Tooltip(widget, text)
    return widget


def show_guide(parent, on_close=None):
    """使用說明視窗（非強制回應，可以一邊看一邊操作）。"""
    win = tk.Toplevel(parent)
    win.title("使用說明")
    win.transient(parent)
    win.geometry("720x720")
    frame = ttk.Frame(win, padding=10)
    frame.pack(fill="both", expand=True)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    # 中文沒有空白可斷行，用 wrap="char"；縮排的段落換行後對齊（懸掛縮排）
    text = tk.Text(frame, wrap="char", font="TkDefaultFont", padx=10, pady=8, relief="flat", spacing1=1, spacing3=1)
    base = tkfont.nametofont("TkDefaultFont")
    text.tag_configure("title", font=(base.actual("family"), 13, "bold"), spacing3=6)
    text.tag_configure("indent", lmargin2=base.measure("   ・"))
    text.tag_configure("heading", font=(base.actual("family"), base.actual("size"), "bold"), spacing1=6)
    for i, line in enumerate(GUIDE_TEXT.splitlines()):
        if i == 0:
            tag = "title"
        elif line[:1].isdigit() or line.startswith("小技巧"):
            tag = "heading"
        elif line.strip():
            tag = "indent"
        else:
            tag = ()
        text.insert("end", line + "\n", tag)
    text.configure(state="disabled")
    text.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    sb.grid(row=0, column=1, sticky="ns")
    text.configure(yscrollcommand=sb.set)

    def close():
        win.destroy()
        if on_close:
            on_close()
    ttk.Button(frame, text="我知道了", command=close).grid(row=1, column=0, columnspan=2, pady=(8, 0))
    win.protocol("WM_DELETE_WINDOW", close)
    return win
