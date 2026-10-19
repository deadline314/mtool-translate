"""核心邏輯自我檢查：python test_core.py"""
import json
import os
import re
import tempfile
import threading

import core


def test_mask_roundtrip():
    s = "\\C[2]アリス\\C[0]は\\V[1]Gを手に入れた！\\.\\|<color=red>%1</color>{0}"
    m, codes = core.mask(s)
    assert "\\" not in m and "<" not in m and "%1" not in m and "{0}" not in m, m
    assert core.unmask(m, codes) == s
    # 模型把佔位符換了順序、加了空白也要能還原
    assert core.unmask("[[ 1 ]][[0]]", ["A", "B"]) == "BA"


def test_mask_terms():
    m, codes = core.mask("アリスとアリスの剣", {"アリス": "愛麗絲", "アリスの剣": "愛麗絲之劍"})
    assert "アリス" not in m
    assert core.unmask(m.replace("と", "和"), codes) == "愛麗絲和愛麗絲之劍"


def test_placeholder_error():
    assert core.placeholder_error("[[0]]a[[1]]", "b[[1]][[0]]") == ""
    assert "遺漏" in core.placeholder_error("[[0]]a[[1]]", "b[[0]]")
    assert "多出" in core.placeholder_error("[[0]]", "[[0]][[0]]")


def test_needs_translation():
    assert core.needs_translation("はい", "ja")
    assert core.needs_translation("攻撃力", "ja")
    assert not core.needs_translation("123 !?", "ja")
    assert not core.needs_translation("\\C[2]", "ja")
    assert not core.needs_translation("OK", "ja")
    assert core.needs_translation("Hello", "en")


def test_parse_json_reply():
    assert core.parse_json_reply('<think>{"x":1}</think>好的\n```json\n{"1": "是"}\n```') == {"1": "是"}


class FakeLLM:
    """假的 LLM：照字典翻譯；「壊れ」這個詞故意弄丟佔位符，用來測重試與保留原文。"""
    llm = True
    DICT = {"はい": "是", "いいえ": "否", "アリス": "愛麗絲", "が来た": "來了", "壊れ": "壞掉"}

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def complete(self, system, user):
        with self.lock:
            self.calls.append(user)
        if "【文本】" in user:  # 術語擷取
            return '{"アリス": "愛麗絲", "不存在的詞": "X"}'
        todo = json.loads(user.split("【待翻譯】\n", 1)[1])
        out = {}
        for i, s in todo.items():
            t = s
            for a, b in self.DICT.items():
                t = t.replace(a, b)
            if "壞掉" in t:
                t = re.sub(r"\[\[\d+\]\]", "", t)
            out[i] = t
        return "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"


def run_job(data, out_path, engine, glossary=None, **cfg):
    cfg = {"src_lang": "ja", "tgt_lang": "zh-TW", "batch_size": 2, "workers": 2,
           "context_lines": 2, "background": "", **cfg}
    progress, logs = [], []
    job = core.Job(engine, cfg, glossary or {}, progress.append, logs.append, threading.Event())
    return job, job.translate(data, out_path), progress, logs


def test_translate_pipeline_and_resume():
    data = {k: k for k in ["はい", "いいえ", "123", "\\C[2]アリス\\C[0]が来た", "\\V[1]壊れ", "はいはい"]}
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out.json")
        eng = FakeLLM()
        _, res, progress, logs = run_job(data, out, eng, glossary={"アリス": "愛麗絲"})
        assert res["はい"] == "是" and res["いいえ"] == "否"
        assert res["123"] == "123"                                   # 跳過
        assert res["\\C[2]アリス\\C[0]が来た"] == "\\C[2]愛麗絲\\C[0]來了"  # 控制碼還原
        assert res["\\V[1]壊れ"] == "\\V[1]壊れ"                       # 弄丟佔位符：重試後仍失敗，保留原文
        assert any("翻譯失敗" in m for m in logs), logs
        assert any("アリス => 愛麗絲" in c for c in eng.calls)           # 術語有送進提示詞
        assert any("【前文】" in c for c in eng.calls)                    # 有上下文
        # 進度：單調遞增、最後剛好等於總數
        dones = [p["done"] for p in progress]
        assert dones == sorted(dones) and progress[-1]["done"] == progress[-1]["total"] == 5
        assert progress[-1]["failed"] == 1 and progress[-1]["skipped"] == 1
        assert core.load_json(out) == res                               # 有存檔

        # 續翻：只剩失敗的那條要重跑
        eng2 = FakeLLM()
        _, res2, progress2, logs2 = run_job(data, out, eng2)
        assert progress2[0]["done"] == 4 and progress2[-1]["done"] == 5
        assert all("はい" not in c.split("【待翻譯】")[1] for c in eng2.calls)


def test_mt_engine_spaces_and_terms():
    class FakeMT:
        llm = False

        def translate(self, text):  # 模擬機翻：在佔位符旁塞空白
            return re.sub(r"(\[\[\d+\]\])", r" \1 ", text.replace("が来た", "來了"))
    data = {"\\C[2]アリス\\C[0]が来た": ""}
    with tempfile.TemporaryDirectory() as d:
        _, res, _, _ = run_job(data, os.path.join(d, "o.json"), FakeMT(), glossary={"アリス": "愛麗絲"})
    assert res["\\C[2]アリス\\C[0]が来た"] == "\\C[2]愛麗絲\\C[0]來了", res


def test_extract_terms():
    data = {"アリスが来た": "", "はい": ""}
    job = core.Job(FakeLLM(), {"src_lang": "ja", "tgt_lang": "zh-TW", "workers": 1},
                   {}, lambda p: None, lambda m: None, threading.Event())
    assert job.extract_terms(data) == {"アリス": "愛麗絲"}  # 原文沒出現的詞被濾掉


def test_fatal_stops_job():
    class Bad:
        llm = True

        def complete(self, s, u):
            raise core.FatalError("HTTP 401")
    data = {f"はい{i}": "" for i in range(10)}
    with tempfile.TemporaryDirectory() as d:
        try:
            run_job(data, os.path.join(d, "o.json"), Bad())
            raise AssertionError("應該要丟 FatalError")
        except core.FatalError:
            pass
        assert os.path.exists(os.path.join(d, "o.json"))  # 中止前仍有存檔


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
