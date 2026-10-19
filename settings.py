"""介面設定的讀寫與舊版設定轉換。設定檔壞掉或欄位不合法時回到預設值，不會讓程式打不開。"""
import logging
import os

from translator import LANGS, PROVIDER_BY_ID, read_json, write_json_atomic

log = logging.getLogger(__name__)

VERSION = 2
DEFAULTS = {
    "version": VERSION,
    "provider": "claude",
    "per_provider": {},
    "src_lang": "ja",
    "tgt_lang": "zh-TW",
    "context_lines": 5,
    "background": "",
    "input": "",
    "output": "",
    "guide_seen": False,
}

# 第 1 版以服務的顯示名稱存設定
_V1_NAMES = {
    "Claude（Anthropic）": "claude", "OpenAI": "openai", "Google Gemini": "gemini", "DeepSeek": "deepseek",
    "自訂（OpenAI 相容）": "custom", "MyMemory": "mymemory", "Google 翻譯（非官方）": "google",
    "Sakura / llama.cpp": "sakura", "Ollama": "ollama", "LM Studio": "lmstudio",
}


def _migrate(raw):
    if raw.get("version", 1) >= VERSION:
        return raw
    out = {k: v for k, v in raw.items() if k in DEFAULTS}
    out["provider"] = _V1_NAMES.get(raw.get("preset"), DEFAULTS["provider"])
    out["per_provider"] = {_V1_NAMES[n]: v for n, v in (raw.get("per_preset") or {}).items() if n in _V1_NAMES}
    out["guide_seen"] = True  # 舊使用者已經用過了
    out["version"] = VERSION
    return out


def _sanitize(s):
    out = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        if isinstance(s.get(k), type(default)):
            out[k] = s[k]
    if out["provider"] not in PROVIDER_BY_ID:
        out["provider"] = DEFAULTS["provider"]
    if out["src_lang"] not in LANGS:
        out["src_lang"] = DEFAULTS["src_lang"]
    if out["tgt_lang"] not in LANGS:
        out["tgt_lang"] = DEFAULTS["tgt_lang"]
    out["per_provider"] = {k: v for k, v in out["per_provider"].items()
                           if k in PROVIDER_BY_ID and isinstance(v, dict)}
    return out


def load_settings(path):
    if not os.path.exists(path):
        return dict(DEFAULTS)
    try:
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise ValueError("不是 JSON 物件")
        return _sanitize(_migrate(raw))
    except (OSError, ValueError) as e:
        log.warning("settings unreadable, using defaults: %s", e)
        return dict(DEFAULTS)


def save_settings(path, data):
    write_json_atomic(path, data)
