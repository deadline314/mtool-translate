"""翻譯引擎。

三種引擎介面，翻譯流程依介面決定怎麼送：
- ChatEngine：一般 LLM。流程組提示詞（術語表、前文、JSON 格式），引擎只負責 complete(system, user)
- LineEngine：自帶固定提示格式的翻譯專用模型（Sakura）。流程給遮罩後的句子與術語，引擎回傳同樣行數的譯文
- MTEngine：機器翻譯，一次一句，沒有上下文（translate）

引擎只負責「送一次請求、把錯誤分類」；重試、退避、停止都由翻譯流程統一處理。
新增引擎：寫一個子類別、在 _FACTORIES 註冊建立方式、在 PROVIDERS 加一筆，介面會自動出現。
"""
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .errors import FatalError, RateLimitError, RequestError, TransientError


class Engine:
    def list_models(self):
        return []


class ChatEngine(Engine):
    def complete(self, system, user):
        raise NotImplementedError


class LineEngine(Engine):
    def translate_lines(self, texts, terms):
        """texts：遮罩後的句子；terms：這批用得到的術語。回傳同樣數量的譯文。"""
        raise NotImplementedError


class MTEngine(Engine):
    def translate(self, text):
        raise NotImplementedError


# ---------------------------------------------------------------- HTTP

def _retry_after(headers):
    try:
        return min(float(headers.get("Retry-After")), 120.0)
    except (TypeError, ValueError, AttributeError):
        return None


def http_json(url, data=None, headers=None, timeout=120):
    """送一次請求並回傳 JSON；把各種失敗分類成 FatalError / RateLimitError / RequestError / TransientError。"""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = {"Content-Type": "application/json", "User-Agent": "mtool-translate", **(headers or {})}
    try:
        req = urllib.request.Request(url, data=body, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300].strip()
        code = e.code
        if code in (401, 403):
            raise FatalError(f"金鑰無效或沒有權限（HTTP {code}）：{detail}") from e
        if code == 404:
            raise FatalError(f"找不到 API 端點或模型（HTTP 404），請檢查位址與模型名稱：{detail}") from e
        if code == 429:
            if "insufficient_quota" in detail:
                raise RateLimitError(f"帳戶額度已用完（HTTP 429），請儲值或換一組金鑰：{detail}") from e
            raise TransientError(f"請求太頻繁（HTTP 429）：{detail}",
                                 retry_after=_retry_after(e.headers), rate_limited=True) from e
        if code in (408, 409) or code >= 500:
            raise TransientError(f"伺服器暫時無法處理（HTTP {code}）：{detail}",
                                 retry_after=_retry_after(e.headers)) from e
        raise RequestError(f"請求被拒絕（HTTP {code}）：{detail}") from e
    except ValueError as e:  # unknown url type 等
        raise FatalError(f"API 位址格式不正確：{url}") from e
    except OSError as e:  # URLError、逾時、連線被拒
        reason = getattr(e, "reason", e)
        raise TransientError(f"連線失敗：{reason}") from e
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TransientError(f"伺服器回應不是 JSON：{raw[:120]!r}") from e


def _check_base_url(base_url):
    base_url = (base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", base_url):
        raise FatalError("請填寫正確的 API 位址（http:// 或 https:// 開頭）")
    return base_url


def _require_model(model):
    if not model:
        raise FatalError("請填寫模型名稱（可按「取得模型清單」選擇）")


# ---------------------------------------------------------------- LLM

class OpenAICompatEngine(ChatEngine):
    """OpenAI 相容的 /chat/completions：OpenAI、Gemini、DeepSeek、LM Studio、llama.cpp、OpenRouter…"""

    def __init__(self, base_url, api_key="", model="", temperature=None, timeout=120):
        self.base = _check_base_url(base_url)
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model, self.temperature, self.timeout = model, temperature, timeout

    def complete(self, system, user):
        _require_model(self.model)
        data = {"model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.temperature is not None:
            data["temperature"] = self.temperature
        r = http_json(self.base + "/chat/completions", data, self.headers, self.timeout)
        try:
            choice = r["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise TransientError(f"回應格式不對：{str(r)[:200]}")
        if choice.get("finish_reason") == "length":
            raise RequestError("輸出超過模型長度上限被截斷")
        if choice.get("finish_reason") == "content_filter":
            raise RequestError("內容被服務的安全過濾擋下")
        return text

    def list_models(self):
        r = http_json(self.base + "/models", headers=self.headers, timeout=20)
        return sorted(m["id"] for m in r.get("data", []) if isinstance(m, dict) and "id" in m)


class OllamaEngine(ChatEngine):
    """Ollama 原生 API。用原生 API 才能設定上下文長度，否則預設長度會默默截掉提示詞。"""
    # ponytail: 固定上下文長度；每次請求改長度會讓 Ollama 重新載入模型，需要更長時再開放設定
    NUM_CTX = 8192

    def __init__(self, base_url, model="", temperature=None, timeout=300):
        base = _check_base_url(base_url)
        self.base = base[:-3] if base.endswith("/v1") else base  # 相容舊設定的 OpenAI 相容位址
        self.model, self.temperature, self.timeout = model, temperature, timeout

    def complete(self, system, user):
        _require_model(self.model)
        options = {"num_ctx": self.NUM_CTX}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        r = http_json(self.base + "/api/chat", {
            "model": self.model, "stream": False, "options": options,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }, timeout=self.timeout)
        if r.get("done_reason") == "length":
            raise RequestError("輸出超過模型長度上限被截斷")
        try:
            return r["message"]["content"] or ""
        except (KeyError, TypeError):
            raise TransientError(f"回應格式不對：{str(r)[:200]}")

    def list_models(self):
        r = http_json(self.base + "/api/tags", timeout=20)
        return sorted(m["name"] for m in r.get("models", []) if isinstance(m, dict) and "name" in m)


class SakuraEngine(LineEngine):
    """
    Sakura 日翻中模型（llama.cpp server 的 OpenAI 相容端點）。
    Sakura 是用固定提示格式訓練的，不能套一般的 JSON 提示；一行一句送出、一行一句收回，輸出為簡體中文。
    """
    SYSTEM = ("你是一个轻小说翻译模型，可以流畅通顺地以日本轻小说的风格将日文翻译成简体中文，"
              "并联系上下文正确使用人称代词，不擅自添加原文中没有的代词。")

    def __init__(self, base_url, api_key="", model="sakura", temperature=0.1, timeout=300):
        self.chat = OpenAICompatEngine(base_url, api_key, model or "sakura", temperature, timeout)

    def _request(self, lines, terms):
        text = "\n".join(lines)
        if terms:
            gpt_dict = "\n".join(f"{k}->{v}" for k, v in terms.items())
            user = f"根据以下术语表（可以为空）：\n{gpt_dict}\n将下面的日文文本根据对应关系和备注翻译成中文：{text}"
        else:
            user = f"将下面的日文文本翻译成中文：{text}"
        reply = self.chat.complete(self.SYSTEM, user).strip("\n")
        if len(lines) == 1:
            return [reply]
        out = reply.split("\n")
        if len(out) != len(lines):
            raise RequestError(f"Sakura 輸出 {len(out)} 行，原文 {len(lines)} 行，對不上")
        return out

    def translate_lines(self, texts, terms):
        out, group = [], []

        def flush():
            if group:
                out.extend(self._request(group, {k: v for k, v in terms.items() if any(k in g for g in group)}))
                group.clear()

        for t in texts:
            if "\n" in t:  # 多行的句子單獨送，才不會跟其他句子的行數混在一起
                flush()
                out.extend(self._request([t], {k: v for k, v in terms.items() if k in t}))
            else:
                group.append(t)
        flush()
        return out

    def list_models(self):
        return self.chat.list_models()


# 這幾個模型支援伺服器端 refusal fallback（被安全機制擋下時自動換模型重跑）
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class ClaudeEngine(ChatEngine):
    def __init__(self, api_key="", model="claude-opus-5-5", base_url="", timeout=120):
        try:
            import anthropic
        except ImportError:
            raise FatalError("缺少 anthropic 套件，請在命令列執行：pip install anthropic")
        self.a = anthropic
        # 重試由翻譯流程統一處理（可被停止鍵中斷），所以 SDK 不自己重試
        self.client = anthropic.Anthropic(api_key=api_key or None, base_url=base_url or None,
                                          max_retries=0, timeout=timeout)
        self.model = model

    def _call(self, fn):
        a = self.a
        try:
            return fn()
        except (a.AuthenticationError, a.PermissionDeniedError) as e:
            raise FatalError(f"金鑰無效或沒有權限：{e.message}") from e
        except a.NotFoundError as e:
            raise FatalError(f"找不到模型或端點，請檢查模型名稱：{e.message}") from e
        except a.RateLimitError as e:
            raise TransientError(f"請求太頻繁：{e.message}",
                                 retry_after=_retry_after(e.response.headers), rate_limited=True) from e
        except a.BadRequestError as e:
            if "credit balance" in str(e.message).lower():
                raise RateLimitError(f"帳戶額度不足，請儲值後再續翻：{e.message}") from e
            raise RequestError(f"請求被拒絕：{e.message}") from e
        except a.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in (408, 409):
                raise TransientError(f"伺服器暫時無法處理（HTTP {e.status_code}）：{e.message}") from e
            raise RequestError(f"請求被拒絕（HTTP {e.status_code}）：{e.message}") from e
        except a.APIConnectionError as e:
            raise TransientError(f"連線失敗：{e}") from e
        except a.AnthropicError as e:  # 例如找不到任何金鑰
            raise FatalError(f"Claude 設定錯誤：{e}") from e

    def complete(self, system, user):
        _require_model(self.model)
        kwargs = dict(model=self.model, max_tokens=16000, system=system,
                      messages=[{"role": "user", "content": user}])
        if self.model in CLAUDE_FALLBACK_MODELS:
            r = self._call(lambda: self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs))
        else:
            r = self._call(lambda: self.client.messages.create(**kwargs))
        if r.stop_reason == "refusal":
            raise RequestError("模型拒絕翻譯這段內容")
        if r.stop_reason == "max_tokens":
            raise RequestError("輸出超過長度上限被截斷")
        return "".join(b.text for b in r.content if b.type == "text")

    def list_models(self):
        return sorted(m.id for m in self._call(lambda: list(self.client.models.list())))


# ---------------------------------------------------------------- 機器翻譯

def _split_long(line, limit):
    """太長的一行在句號處切開；沒有句號就硬切。"""
    parts, buf = [], ""
    for piece in re.split(r"(?<=[。！？!?\.])", line):
        while len(piece) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(piece[:limit])
            piece = piece[limit:]
        if buf and len(buf) + len(piece) > limit:
            parts.append(buf)
            buf = ""
        buf += piece
    if buf:
        parts.append(buf)
    return parts


class MyMemoryEngine(MTEngine):
    """MyMemory 公開免費 API。匿名每天約 5,000 字，填 Email 可提高到約 50,000 字。"""
    URL = "https://api.mymemory.translated.net/get"
    MAX_CHARS = 480  # 單次請求上限約 500 字

    def __init__(self, src_lang, tgt_lang, email="", timeout=30):
        self.pair, self.email, self.timeout = f"{src_lang}|{tgt_lang}", (email or "").strip(), timeout

    def _one(self, text):
        params = {"q": text, "langpair": self.pair}
        if self.email:
            params["de"] = self.email
        r = http_json(self.URL + "?" + urllib.parse.urlencode(params), timeout=self.timeout)
        status = str(r.get("responseStatus"))
        if r.get("quotaFinished") or status == "429":
            raise RateLimitError("MyMemory 今日免費額度已用完（填 Email 可提高額度，或明天再續翻）")
        if status != "200":
            raise RequestError(f"MyMemory：{r.get('responseDetails')}")
        return html.unescape(r["responseData"]["translatedText"])

    def translate(self, text):
        if len(text) <= self.MAX_CHARS:
            return self._one(text)
        out = []
        for line in text.split("\n"):
            if not line.strip():
                out.append(line)
            else:
                out.append("".join(self._one(p) for p in _split_long(line, self.MAX_CHARS)))
        return "\n".join(out)


class GoogleFreeEngine(MTEngine):
    """Google 翻譯網頁版的免費端點。非官方，量大或某些網路環境會被擋。"""
    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, src_lang, tgt_lang, timeout=30):
        self.sl, self.tl, self.timeout = src_lang or "auto", tgt_lang, timeout

    def translate(self, text):
        q = urllib.parse.urlencode({"client": "gtx", "sl": self.sl, "tl": self.tl, "dt": "t", "q": text})
        r = http_json(self.URL + "?" + q, timeout=self.timeout)
        try:
            return "".join(seg[0] for seg in r[0] if seg and seg[0])
        except (TypeError, IndexError, KeyError):
            raise TransientError(f"Google 回應格式不對：{str(r)[:120]}")


# ---------------------------------------------------------------- 服務清單（介面依這份資料產生）

CATEGORIES = ["雲端 API", "免費服務", "本地 AI", "本地 LLM"]


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    category: str
    kind: str                     # 對應 _FACTORIES 的鍵
    help: str                     # 介面上的說明（hover 與提示列）
    base_url: str = ""
    model: str = ""
    temperature: float = None
    workers: int = 1
    batch: int = 10
    timeout: int = 120
    key_label: str = "API Key"
    fields: frozenset = field(default=frozenset({"url", "key", "model"}))  # 介面要顯示哪些欄位

    @property
    def supports_terms_extraction(self):
        return self.kind in ("openai", "claude", "ollama")


_FACTORIES = {
    "openai": lambda p, url, key, model, src, tgt: OpenAICompatEngine(url, key, model, p.temperature, p.timeout),
    "ollama": lambda p, url, key, model, src, tgt: OllamaEngine(url, model, p.temperature, p.timeout),
    "sakura": lambda p, url, key, model, src, tgt: SakuraEngine(url, key, model, p.temperature, p.timeout),
    "claude": lambda p, url, key, model, src, tgt: ClaudeEngine(key, model, timeout=p.timeout),
    "mymemory": lambda p, url, key, model, src, tgt: MyMemoryEngine(src, tgt, key, p.timeout),
    "google": lambda p, url, key, model, src, tgt: GoogleFreeEngine(src, tgt, p.timeout),
}

PROVIDERS = [
    Provider("claude", "Claude（Anthropic）", "雲端 API", "claude",
             "Anthropic 官方 API，翻譯品質最好。大量翻譯可改用 claude-haiku-5-5 或 claude-sonnet-5-5 降低成本。"
             "金鑰留空會讀環境變數 ANTHROPIC_API_KEY。",
             model="claude-opus-5-5", workers=4, batch=25, fields=frozenset({"key", "model"})),
    Provider("openai", "OpenAI", "雲端 API", "openai",
             "OpenAI 官方 API。預設模型名稱可能過時，可按「取得模型清單」。",
             base_url="https://api.openai.com/v1", model="gpt-4.1-mini", workers=4, batch=25),
    Provider("gemini", "Google Gemini", "雲端 API", "openai",
             "Gemini 的 OpenAI 相容端點，金鑰在 Google AI Studio 申請。",
             base_url="https://generativelanguage.googleapis.com/v1beta/openai", model="gemini-2.5-flash",
             workers=4, batch=25),
    Provider("deepseek", "DeepSeek", "雲端 API", "openai",
             "DeepSeek 官方 API，價格便宜、中文品質不錯。",
             base_url="https://api.deepseek.com/v1", model="deepseek-chat", workers=4, batch=25),
    Provider("custom", "自訂（OpenAI 相容）", "雲端 API", "openai",
             "任何相容 OpenAI /chat/completions 的服務，例如 OpenRouter、自架代理。位址填到 /v1 為止。",
             base_url="https://", workers=2, batch=20),
    Provider("mymemory", "MyMemory", "免費服務", "mymemory",
             "免費、不需註冊。匿名每天約 5,000 字，填 Email 可提高到約 50,000 字。"
             "沒有上下文能力，術語以直接替換的方式套用。",
             key_label="Email（選填）", workers=2, batch=10, timeout=30, fields=frozenset({"key"})),
    Provider("google", "Google 翻譯（非官方）", "免費服務", "google",
             "Google 翻譯網頁版的非官方端點，量大或某些網路環境會被擋（HTTP 429）。沒有上下文能力。",
             workers=2, batch=10, timeout=30, fields=frozenset()),
    Provider("sakura", "Sakura（llama.cpp）", "本地 AI", "sakura",
             "日翻中專用模型，輸出為簡體中文。先用 llama.cpp 的 llama-server 載入 Sakura 模型再連線，金鑰留空。"
             "Sakura 有自己的提示格式，不使用前文參考與遊戲背景。",
             base_url="http://localhost:8080/v1", model="sakura", temperature=0.1, timeout=300),
    Provider("ollama", "Ollama", "本地 LLM", "ollama",
             "先安裝 Ollama 並下載模型（例如在命令列執行 ollama pull qwen2.5:14b），不需要金鑰。",
             base_url="http://localhost:11434", model="qwen2.5:14b", temperature=0.2, timeout=300,
             fields=frozenset({"url", "model"})),
    Provider("lmstudio", "LM Studio", "本地 LLM", "openai",
             "在 LM Studio 開啟 Local Server 並載入模型，金鑰留空；模型名稱可按「取得模型清單」。",
             base_url="http://localhost:1234/v1", temperature=0.2, timeout=300),
]
PROVIDER_BY_ID = {p.id: p for p in PROVIDERS}


def make_engine(provider, base_url="", api_key="", model="", src_lang="ja", tgt_lang="zh-TW"):
    """依服務設定建立引擎。設定有誤時丟 FatalError。"""
    return _FACTORIES[provider.kind](provider, base_url, (api_key or "").strip(), (model or "").strip(),
                                     src_lang, tgt_lang)
