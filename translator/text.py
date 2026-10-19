"""文字處理：控制碼遮罩、判斷要不要翻、解析模型回應、術語表整理。全部是純函式。"""
import json
import re
from collections import Counter

LANGS = {
    "ja": "日文",
    "en": "英文",
    "zh-CN": "簡體中文",
    "zh-TW": "繁體中文（台灣用語）",
    "ko": "韓文",
}

# ---------------------------------------------------------------- 控制碼遮罩

CODE_RE = re.compile(
    r"\[\[\s*\d+\s*\]\]"                        # 原文本來就長得像佔位符的，也保護起來
    r"|\\[A-Za-z]+\[[^\]\n]*\]"                 # \C[2] \V[1] \N[1] \I[64] \FS[24]
    r"|\\[A-Za-z]+<[^>\n]*>"                    # \n<名字>（YEP 系插件）
    r"|\\[{}\\$.|!><^]"                         # \{ \} \\ \$ \. \| \! \> \< \^
    r"|\\[A-Za-z]+"                             # \G 等不帶參數的控制碼
    r"|</?[A-Za-z][^<>\n]{0,40}>"               # <color=red> </b> <WordWrap>；<勇者の剣> 這種日文不算標籤
    r"|\{/?[A-Za-z_][\w.]*(?:[=:][^{}\n]*)?\}"  # {name} {w=0.5} {/b} {player.name}
    r"|\{\d+(?::[^{}\n]*)?\}"                   # {0} {0:N0}
    r"|\[[A-Za-z_][\w.]*(?:[ =][^\[\]\n]*)?\]"  # Ren'Py [name]、Tyrano/KiriKiri [r] [l] [ruby text=…]
    r"|%[-+0#]*\d*(?:\.\d+)?[sdifxX]|%\d+"       # %s %d %02d %.1f %1（不含空白旗標，免得吃掉「50% discount」）
)
# Wolf RPG / MZ 的注音 \r[本文,ふりがな]：只留本文給翻譯，注音對譯文沒有意義
RUBY_RE = re.compile(r"\\r\[([^,\]\n]+),[^\]\n]*\]")
PH_RE = re.compile(r"\[\[\s*(\d+)\s*\]\]")
_PH_SPACES_RE = re.compile(r" *(\[\[\s*\d+\s*\]\]) *")  # 只動半形空白；全形縮排是原文就有的


def mask(text, terms=None):
    """控制碼換成 [[i]]。給 terms={原文: 譯名} 時連術語也換掉（機翻用），還原時直接填入譯名。"""
    codes = []

    def sub(m):
        codes.append(m.group(0))
        return f"[[{len(codes) - 1}]]"

    out = CODE_RE.sub(sub, RUBY_RE.sub(r"\1", text))
    for src in sorted(terms or {}, key=len, reverse=True):
        if src and src in out:
            codes.append(terms[src])
            out = out.replace(src, f"[[{len(codes) - 1}]]")
    return out, codes


def unmask(text, codes):
    def sub(m):
        i = int(m.group(1))
        return codes[i] if i < len(codes) else m.group(0)
    return PH_RE.sub(sub, text)


def strip_placeholder_spaces(text):
    """機翻常在佔位符兩側塞空白，中日文不需要。"""
    return _PH_SPACES_RE.sub(r"\1", text)


def placeholder_error(masked_src, translated):
    """佔位符必須一個不少、一個不多。回傳錯誤訊息，沒問題回傳空字串。"""
    want, got = Counter(PH_RE.findall(masked_src)), Counter(PH_RE.findall(translated))
    if want == got:
        return ""
    parts = []
    missing, extra = sorted((want - got).elements()), sorted((got - want).elements())
    if missing:
        parts.append("遺漏控制碼 " + " ".join(f"[[{i}]]" for i in missing))
    if extra:
        parts.append("多出控制碼 " + " ".join(f"[[{i}]]" for i in extra))
    return "；".join(parts)


def term_error(src, translated, glossary):
    """原文出現術語、譯文卻沒用指定譯名。"""
    miss = [f"{k}→{v}" for k, v in glossary.items() if k in src and v not in translated]
    return ("未使用術語譯名 " + "、".join(miss)) if miss else ""


# ---------------------------------------------------------------- 判斷要不要翻

_SCRIPT_RE = {
    "ja": re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]"),
    "zh-CN": re.compile(r"[\u3400-\u9fff]"),
    "zh-TW": re.compile(r"[\u3400-\u9fff]"),
    "ko": re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"),
}


def needs_translation(text, src_lang):
    plain = CODE_RE.sub("", RUBY_RE.sub(r"\1", text))
    rx = _SCRIPT_RE.get(src_lang)
    if rx:
        return bool(rx.search(plain))
    return any(c.isalpha() for c in plain)


# ---------------------------------------------------------------- 模型回應

_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_DECODER = json.JSONDecoder()


def _last_json_object(text):
    """從左到右試著解析每個 {，回傳最後一個完整的 JSON 物件。"""
    found, i = None, text.find("{")
    while i >= 0:
        try:
            obj, end = _DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = text.find("{", end)
    return found


def parse_json_reply(text):
    """
    從模型回應抓出 JSON 物件。容忍：思考過程（含只有 </think> 結尾的）、```json 區塊、
    前後廢話與其中的大括號、結尾多餘逗號、外層多包一層。
    """
    text = text or ""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    obj = _last_json_object(text)
    if obj is None:
        obj = _last_json_object(_TRAILING_COMMA_RE.sub(r"\1", text))
    if obj is None:
        raise ValueError("回應裡找不到 JSON 物件")
    if len(obj) == 1:  # {"translations": {...}} 這種多包一層的
        only = next(iter(obj.values()))
        if isinstance(only, dict):
            obj = only
    return obj


# ---------------------------------------------------------------- 術語表

def normalize_glossary(obj):
    """只留 {非空字串: 非空字串}，去頭尾空白。讀檔、匯入、模型回傳都經過這裡。"""
    if not isinstance(obj, dict):
        raise ValueError("術語表必須是 {\"原文\": \"譯名\"} 格式的 JSON 物件")
    out = {}
    for k, v in obj.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def relevant_terms(glossary, texts):
    """只挑這批文字裡真的出現的術語，避免提示詞塞滿用不到的詞。"""
    blob = "\n".join(texts)
    return {k: v for k, v in glossary.items() if k in blob}
