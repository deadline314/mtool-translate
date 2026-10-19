"""翻譯檔格式。翻譯流程只處理「原文清單 → {原文: 譯文}」，讀寫哪種檔案由格式類別決定。

新增格式（例如 Unity XUnity 的 txt）：繼承 TranslationFormat 實作三個方法，加進 FORMATS。
"""
import json
import os
import threading
import time

from .errors import FatalError


def read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _dumps(data):
    """{字串: 字串} 每條一行：比 json.dump(indent=…) 快很多（逐條走 C 編碼器），檔案也方便人看。"""
    if isinstance(data, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        enc = json.JSONEncoder(ensure_ascii=False).encode
        return "{\n" + ",\n".join(f" {enc(k)}: {enc(v)}" for k, v in data.items()) + "\n}\n"
    return json.dumps(data, ensure_ascii=False, indent=1)


def write_json_atomic(path, data):
    """先寫暫存檔再取代：寫到一半當掉也不會弄壞原本的檔案。目標被別的程式暫時鎖住時短暫重試。"""
    if not path:
        raise OSError("沒有指定輸出檔路徑")
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    text = _dumps(data)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class TranslationFormat:
    name = ""
    patterns = ()

    def load_source(self, path):
        """回傳要翻譯的原文清單（保留原順序）。"""
        raise NotImplementedError

    def load_existing(self, path):
        """讀既有輸出檔，回傳 {原文: 譯文}；檔案不存在回傳空 dict。檔案損壞要丟錯，絕不能默默覆蓋。"""
        raise NotImplementedError

    def save(self, path, mapping):
        raise NotImplementedError


class MToolJsonFormat(TranslationFormat):
    """MTool 的 ManualTransFile.json：{"原文": "原文"}；翻譯檔：{"原文": "譯文"}。"""
    name = "MTool 翻譯檔（JSON）"
    patterns = ("*.json",)

    def load_source(self, path):
        try:
            data = read_json(path)
        except FileNotFoundError:
            raise FatalError(f"找不到原文檔：{path}")
        except (OSError, ValueError) as e:
            raise FatalError(f"原文檔不是有效的 JSON：{e}")
        if not isinstance(data, dict):
            raise FatalError("原文檔格式不對：應該是 MTool 匯出的 {\"原文\": \"原文\"} 物件")
        return [k for k in data if isinstance(k, str)]

    def load_existing(self, path):
        if not path or not os.path.exists(path):
            return {}
        try:
            data = read_json(path)
        except (OSError, ValueError) as e:
            raise FatalError(f"既有的輸出檔無法讀取，為避免覆蓋請先改名或移走：{path}\n{e}")
        if not isinstance(data, dict):
            raise FatalError(f"既有的輸出檔格式不對，為避免覆蓋請先改名或移走：{path}")
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def save(self, path, mapping):
        write_json_atomic(path, mapping)


MTOOL_JSON = MToolJsonFormat()
FORMATS = [MTOOL_JSON]
