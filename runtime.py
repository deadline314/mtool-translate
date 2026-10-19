"""執行環境：設定與記錄放哪、打包後資源檔在哪、打包後的自我檢查（python app.py --self-check）。"""
import os
import ssl
import sys
import tempfile
import traceback

APP_ID = "MToolTranslator"
FROZEN = getattr(sys, "frozen", False)
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def _writable(folder):
    try:
        probe = os.path.join(folder, f".write-test-{os.getpid()}")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def app_dir():
    """
    設定檔與記錄的存放位置。打包後放在 exe 旁邊（整個資料夾帶著走就好）；
    exe 放在不能寫入的地方（例如 Program Files）時改放 %APPDATA%\\MToolTranslator。
    """
    base = os.path.dirname(sys.executable) if FROZEN else SOURCE_DIR
    if _writable(base):
        return base
    alt = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_ID)
    os.makedirs(alt, exist_ok=True)
    return alt


def resource_path(rel):
    """隨程式一起打包的檔案（例如圖示）。"""
    return os.path.join(getattr(sys, "_MEIPASS", SOURCE_DIR), rel)


def self_check(out_dir, extra_checks=()):
    """
    打包後確認每個元件都在、每個引擎都建得起來，並跑 extra_checks（名稱, 函式）。
    結果寫到 self-check.txt，全部通過回傳 0。
    """
    from translator import PROVIDERS, MTOOL_JSON, make_engine
    from translator.text import mask, unmask

    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"OK    {name}" + (f"：{detail}" if detail else ""))
        except Exception as e:
            ok = False
            lines.append(f"FAIL  {name}：{type(e).__name__}: {e}")
            lines.append(traceback.format_exc())

    def tk_check():
        import tkinter
        tkinter.Tcl().eval("info patchlevel")

    def icon_check():
        path = resource_path(os.path.join("assets", "app.ico"))
        if not os.path.exists(path):
            raise FileNotFoundError(path)

    def engine_check(p):
        url = p.base_url if p.base_url.startswith("http") and p.base_url != "https://" else "http://localhost"
        make_engine(p, url, "test-key", p.model or "test-model")

    def json_check():
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            MTOOL_JSON.save(path, {"はい": "是"})
            assert MTOOL_JSON.load_existing(path) == {"はい": "是"}

    def certs_check():
        n = ssl.create_default_context().cert_store_stats()["x509_ca"]
        if n == 0:
            raise RuntimeError("找不到任何 HTTPS 根憑證，雲端服務會連不上")
        return f"系統憑證 {n} 張"

    check("tkinter", tk_check)
    check("圖示", icon_check)
    check("anthropic 套件", lambda: __import__("anthropic").__version__)
    check("HTTPS 憑證", certs_check)
    check("控制碼遮罩", lambda: None if unmask(*mask("\\C[2]テスト")) == "\\C[2]テスト" else 1 / 0)
    check("翻譯檔讀寫", json_check)
    for p in PROVIDERS:
        check(f"引擎：{p.name}", lambda p=p: engine_check(p))
    for name, fn in extra_checks:
        check(name, fn)
    lines.insert(0, f"{'全部通過' if ok else '有項目失敗'}（{'打包版' if FROZEN else '原始碼'}，Python {sys.version.split()[0]}）")
    with open(os.path.join(out_dir, "self-check.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1
