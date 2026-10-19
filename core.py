"""MTool 翻譯核心：控制碼遮罩、術語表、翻譯引擎、批次翻譯流程。不依賴 UI。"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------- 控制碼遮罩

CODE_RE = re.compile(
    r"\\[A-Za-z]+\[[^\]\n]*\]"   # \C[2] \V[1] \N[1] \I[64] \FS[24]
    r"|\\[A-Za-z]+<[^>\n]*>"     # \n<名字>（YEP 系插件）
    r"|\\[{}\\$.|!><^]"          # \{ \} \\ \$ \. \| \! \> \< \^
    r"|\\[A-Za-z]+"              # \G 等不帶參數的控制碼
    r"|<[^<>\n]{1,40}>"          # <color=red> <b> 等標籤
    r"|%\d+|%[sd]|\{\d+\}"       # %1 %s {0}
    r"|\[\[\s*\d+\s*\]\]"        # 原文本來就長得像佔位符的，也保護起來
)
PH_RE = re.compile(r"\[\[\s*(\d+)\s*\]\]")


def mask(text, terms=None):
    """控制碼換成 [[i]]。給 terms={原文: 譯名} 時連術語也換掉（機翻用），還原時直接填入譯名。"""
    codes = []

    def sub(m):
        codes.append(m.group(0))
        return f"[[{len(codes) - 1}]]"

    out = CODE_RE.sub(sub, text)
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


def placeholder_error(masked_src, translated):
    """佔位符必須一個不少、一個不多。回傳錯誤訊息，沒問題回傳空字串。"""
    want, got = Counter(PH_RE.findall(masked_src)), Counter(PH_RE.findall(translated))
    if want == got:
        return ""
    missing = sorted((want - got).elements())
    extra = sorted((got - want).elements())
    parts = []
    if missing:
        parts.append("遺漏佔位符 " + " ".join(f"[[{i}]]" for i in missing))
    if extra:
        parts.append("多出佔位符 " + " ".join(f"[[{i}]]" for i in extra))
    return "；".join(parts)


def term_error(src, translated, glossary):
    """原文出現術語、譯文卻沒用指定譯名。"""
    miss = [f"{k}→{v}" for k, v in glossary.items() if k and v and k in src and v not in translated]
    return ("未使用術語譯名 " + "、".join(miss)) if miss else ""


# ---------------------------------------------------------------- 判斷要不要翻

SCRIPT_RE = {
    "ja": re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]"),
    "zh-CN": re.compile(r"[\u3400-\u9fff]"),
    "zh-TW": re.compile(r"[\u3400-\u9fff]"),
    "ko": re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"),
}


def needs_translation(text, src_lang):
    plain = CODE_RE.sub("", text)
    rx = SCRIPT_RE.get(src_lang)
    if rx:
        return bool(rx.search(plain))
    return any(c.isalpha() for c in plain)


# ---------------------------------------------------------------- 語言

LANGS = {
    "ja": "日文",
    "en": "英文",
    "zh-CN": "簡體中文",
    "zh-TW": "繁體中文（台灣用語）",
    "ko": "韓文",
}


# ---------------------------------------------------------------- 翻譯引擎

class FatalError(Exception):
    """設定錯誤（金鑰錯、模型不存在…），重試沒有意義，直接停掉整個工作。"""


def _http(url, data=None, headers=None, timeout=300, retries=3):
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (400, 401, 403, 404, 422):
                raise FatalError(f"HTTP {e.code}：{detail}") from e
            if attempt == retries:
                raise RuntimeError(f"HTTP {e.code}：{detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == retries:
                raise RuntimeError(f"連線失敗：{e}") from e
        time.sleep(2 ** (attempt + 1))


class OpenAICompatEngine:
    """OpenAI 相容的 /chat/completions：OpenAI、Gemini、DeepSeek、Ollama、LM Studio、llama.cpp（Sakura）都走這個。"""
    llm = True

    def __init__(self, base_url, api_key="", model="", temperature=None):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.model, self.temperature = model, temperature

    def complete(self, system, user):
        data = {"model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.temperature is not None:
            data["temperature"] = self.temperature
        r = _http(self.base + "/chat/completions", data, self.headers)
        try:
            return r["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"回應格式不對：{str(r)[:300]}")

    def list_models(self):
        r = _http(self.base + "/models", headers=self.headers, timeout=20, retries=0)
        return sorted(m["id"] for m in r.get("data", []))


# 這幾個模型支援伺服器端 refusal fallback（被安全機制擋下時自動換模型重跑）
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class ClaudeEngine:
    llm = True

    def __init__(self, api_key="", model="claude-opus-5-5"):
        try:
            import anthropic
        except ImportError:
            raise FatalError("缺少 anthropic 套件，請執行：pip install anthropic")
        self.anthropic = anthropic
        # 沒填金鑰就讓 SDK 自己找 ANTHROPIC_API_KEY 等環境設定
        self.client = anthropic.Anthropic(api_key=api_key or None, max_retries=3)
        self.model = model

    def complete(self, system, user):
        a = self.anthropic
        kwargs = dict(model=self.model, max_tokens=16000, system=system,
                      messages=[{"role": "user", "content": user}])
        try:
            if self.model in CLAUDE_FALLBACK_MODELS:
                r = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                r = self.client.messages.create(**kwargs)
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError, a.BadRequestError) as e:
            raise FatalError(str(e)) from e
        except a.APIStatusError as e:
            raise RuntimeError(f"HTTP {e.status_code}：{e.message}") from e
        except a.APIConnectionError as e:
            raise RuntimeError(f"連線失敗：{e}") from e
        if r.stop_reason == "refusal":
            raise RuntimeError("模型拒絕翻譯這段內容")
        return "".join(b.text for b in r.content if b.type == "text")

    def list_models(self):
        return sorted(m.id for m in self.client.models.list())


class GoogleFreeEngine:
    """Google 翻譯網頁版的免費端點。非官方、無上下文，量大可能被暫時封鎖。"""
    llm = False
    CODES = {"zh-TW": "zh-TW", "zh-CN": "zh-CN", "ja": "ja", "en": "en", "ko": "ko"}

    def __init__(self, src_lang, tgt_lang):
        self.sl, self.tl = self.CODES.get(src_lang, "auto"), self.CODES.get(tgt_lang, "zh-TW")

    def translate(self, text):
        q = urllib.parse.urlencode({"client": "gtx", "sl": self.sl, "tl": self.tl, "dt": "t", "q": text})
        r = _http("https://translate.googleapis.com/translate_a/single?" + q, timeout=30)
        return "".join(seg[0] for seg in r[0] if seg and seg[0])

    def list_models(self):
        return []


class MyMemoryEngine:
    """MyMemory 公開免費 API。匿名每天約 5,000 字，填 Email 可提高到約 50,000 字。無上下文。"""
    llm = False
    MAX_CHARS = 480  # 單次請求上限約 500 字

    def __init__(self, src_lang, tgt_lang, email=""):
        self.pair = f"{src_lang}|{tgt_lang}"
        self.email = email.strip()

    def _one(self, text):
        params = {"q": text, "langpair": self.pair}
        if self.email:
            params["de"] = self.email
        r = _http("https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(params), timeout=30)
        if r.get("quotaFinished") or str(r.get("responseStatus")) == "429":
            raise FatalError("MyMemory 今日免費額度已用完（可填 Email 提高額度，或明天再續翻）")
        if str(r.get("responseStatus")) != "200":
            raise RuntimeError(f"MyMemory：{r.get('responseDetails')}")
        return r["responseData"]["translatedText"]

    def translate(self, text):
        if len(text) <= self.MAX_CHARS:
            return self._one(text)
        return "\n".join(self._one(line) if line.strip() else line for line in text.split("\n"))

    def list_models(self):
        return []


def make_engine(kind, base_url="", api_key="", model="", temperature=None, src_lang="ja", tgt_lang="zh-TW"):
    if kind == "claude":
        return ClaudeEngine(api_key, model)
    if kind == "google":
        return GoogleFreeEngine(src_lang, tgt_lang)
    if kind == "mymemory":
        return MyMemoryEngine(src_lang, tgt_lang, api_key)
    return OpenAICompatEngine(base_url, api_key, model, temperature)


# ---------------------------------------------------------------- 提示詞

TRANSLATE_SYSTEM = """你是專業的遊戲在地化譯者，把{src}遊戲文本翻譯成{tgt}。
規則：
1. [[數字]] 是遊戲控制碼的佔位符，必須原樣保留在譯文裡，數量不可增減，可依語序調整位置。
2. 術語表裡的詞一律使用指定譯名，全篇保持一致。
3. 盡量保留原文的換行位置；不要加任何解釋或註解。
4. 不在術語表裡的人名、地名、招式、物品名，採自然的音譯或意譯。
5. 「前文」只用來理解語境與說話者，不要翻譯它。
6. 只輸出一個 JSON 物件：鍵與「待翻譯」相同，值為譯文。{background}"""

TERMS_SYSTEM = """你是遊戲在地化的術語專家。從{src}遊戲文本中找出需要統一譯名的專有名詞：角色名、地名、組織、種族、物品、技能、魔法、稱號、遊戲特有用語。
不要收錄一般詞彙、整句台詞或控制碼。
為每個詞給出{tgt}譯名；「已知術語」已有譯名，不要重複列出。
只輸出一個 JSON 物件：{{"原文": "譯名"}}，沒有就輸出 {{}}。{background}"""


def _bg(background):
    return f"\n\n遊戲背景（供參考）：\n{background.strip()}" if background and background.strip() else ""


def parse_json_reply(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("回應裡找不到 JSON")
    obj = json.loads(text[a:b + 1])
    if not isinstance(obj, dict):
        raise ValueError("回應的 JSON 不是物件")
    return obj


def relevant_terms(glossary, texts):
    blob = "\n".join(texts)
    return {k: v for k, v in glossary.items() if k and v and k in blob}


# ---------------------------------------------------------------- 檔案

def load_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path, data):
    """先寫暫存檔再取代，寫到一半當掉也不會弄壞原本的檔案。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 翻譯流程

class Job:
    """
    cfg: src_lang, tgt_lang, batch_size, workers, context_lines, background
    on_progress(dict)：phase, done, total, skipped, failed, elapsed, rate, eta
    on_log(str)
    stop：threading.Event，設定後停止送出新批次，已送出的跑完後存檔結束。
    """

    MAX_BATCH_CHARS = 3000

    def __init__(self, engine, cfg, glossary, on_progress, on_log, stop):
        self.engine, self.cfg, self.glossary = engine, cfg, glossary
        self.on_progress, self.on_log, self.stop = on_progress, on_log, stop

    # ---- 共用

    def _batches(self, items, size):
        batch, chars = [], 0
        for k in items:
            if batch and (len(batch) >= size or chars + len(k) > self.MAX_BATCH_CHARS):
                yield batch
                batch, chars = [], 0
            batch.append(k)
            chars += len(k)
        if batch:
            yield batch

    def _emit(self, phase, done, total, base, t0, skipped=0, failed=0):
        elapsed = time.time() - t0
        new = done - base
        rate = new / elapsed if elapsed > 0 and new > 0 else 0.0
        eta = (total - done) / rate if rate else None
        self.on_progress(dict(phase=phase, done=done, total=total, skipped=skipped, failed=failed,
                              elapsed=elapsed, rate=rate, eta=eta))

    def _run_pool(self, jobs, fn, on_result):
        """jobs 依序送進執行緒池；stop 時取消還沒開始的。"""
        pool = ThreadPoolExecutor(max_workers=max(1, int(self.cfg.get("workers", 1))))
        try:
            futures = {pool.submit(fn, j): j for j in jobs}
            for fut in as_completed(futures):
                if self.stop.is_set():
                    for f in futures:
                        f.cancel()
                if fut.cancelled():
                    continue
                exc = fut.exception()
                if isinstance(exc, FatalError):
                    self.stop.set()
                    for f in futures:
                        f.cancel()
                    raise exc
                on_result(futures[fut], fut.result() if exc is None else exc)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    # ---- 術語擷取

    def extract_terms(self, data):
        cfg = self.cfg
        lines = [k for k in data if needs_translation(k, cfg["src_lang"])]
        chunks = list(self._batches(lines, 10 ** 9))  # 只依字數切
        system = TERMS_SYSTEM.format(src=LANGS[cfg["src_lang"]], tgt=LANGS[cfg["tgt_lang"]],
                                     background=_bg(cfg.get("background")))
        found, t0, done = {}, time.time(), 0
        self._emit("擷取術語", 0, len(chunks), 0, t0)

        def work(chunk):
            masked = [mask(k)[0] for k in chunk]
            known = relevant_terms({**self.glossary, **found}, chunk)
            user = ""
            if known:
                user += "【已知術語】\n" + "\n".join(f"{k} => {v}" for k, v in known.items()) + "\n\n"
            user += "【文本】\n" + "\n".join(masked)
            return parse_json_reply(self.engine.complete(system, user))

        def on_result(chunk, res):
            nonlocal done
            done += 1
            if isinstance(res, Exception):
                self.on_log(f"術語擷取失敗（第 {done} 段）：{res}")
            else:
                blob = "\n".join(chunk)
                for k, v in res.items():
                    k, v = str(k).strip(), str(v).strip()
                    # 只收原文真的出現過的詞，避免模型自己編
                    if k and v and k in blob and k not in self.glossary and k not in found:
                        found[k] = v
            self._emit("擷取術語", done, len(chunks), 0, t0)

        self._run_pool(chunks, work, on_result)
        return found

    # ---- 翻譯

    def _llm_items(self, items, context, note=""):
        """翻一批。回傳 ({原文: 譯文}, {原文: (是否只是術語問題, 錯誤訊息, 候選譯文)})。"""
        cfg = self.cfg
        masked = {str(i + 1): mask(k) for i, k in enumerate(items)}
        terms = relevant_terms(self.glossary, items + [c for c, _ in context])
        system = TRANSLATE_SYSTEM.format(src=LANGS[cfg["src_lang"]], tgt=LANGS[cfg["tgt_lang"]],
                                         background=_bg(cfg.get("background")))
        user = ""
        if terms:
            user += "【術語表】\n" + "\n".join(f"{k} => {v}" for k, v in terms.items()) + "\n\n"
        if context:
            user += "【前文】\n" + "\n".join(f"{mask(s)[0]} => {mask(t)[0]}" if t else mask(s)[0]
                                            for s, t in context) + "\n\n"
        if note:
            user += f"【注意】上一次翻譯有問題：{note}\n\n"
        user += "【待翻譯】\n" + json.dumps({i: m for i, (m, _) in masked.items()}, ensure_ascii=False, indent=0)

        try:
            reply = parse_json_reply(self.engine.complete(system, user))
        except (ValueError, json.JSONDecodeError) as e:
            return {}, {k: (False, f"回應無法解析：{e}", None) for k in items}

        out, errs = {}, {}
        for i, k in enumerate(items):
            m, codes = masked[str(i + 1)]
            t = reply.get(str(i + 1))
            if not isinstance(t, str) or not t.strip():
                errs[k] = (False, "回應缺少這一條", None)
                continue
            err = placeholder_error(m, t)
            if err:
                errs[k] = (False, err, None)
                continue
            final = unmask(t, codes)
            err = term_error(k, final, terms)
            if err:
                errs[k] = (True, err, final)
            else:
                out[k] = final
        return out, errs

    def _translate_batch(self, batch, context):
        """整批翻一次，失敗的逐條重試一次。回傳 ({原文: 譯文}, [警告/錯誤訊息])。"""
        if not self.engine.llm:
            return self._mt_batch(batch)
        out, errs = self._llm_items(batch, context)
        msgs = []
        for k, (soft, err, cand) in errs.items():
            if self.stop.is_set():
                break
            o2, e2 = self._llm_items([k], context, note=err)
            if k in o2:
                out[k] = o2[k]
            elif e2[k][0]:          # 重試後還是只有術語問題：收下，但留警告
                out[k] = e2[k][2]
                msgs.append(f"術語未套用：{k[:30]} ｜ {e2[k][1]}")
            elif soft:              # 重試反而更糟：用第一次的版本
                out[k] = cand
                msgs.append(f"術語未套用：{k[:30]} ｜ {err}")
            else:
                msgs.append(f"翻譯失敗，保留原文：{k[:30]} ｜ {e2[k][1]}")
        return out, msgs

    def _mt_batch(self, batch):
        out, msgs = {}, []
        for k in batch:
            if self.stop.is_set():
                break
            m, codes = mask(k, relevant_terms(self.glossary, [k]))
            try:
                t = self.engine.translate(m)
            except FatalError:
                raise
            except Exception as e:
                msgs.append(f"翻譯失敗，保留原文：{k[:30]} ｜ {e}")
                continue
            err = placeholder_error(m, t)
            if err:
                msgs.append(f"翻譯失敗，保留原文：{k[:30]} ｜ {err}")
                continue
            if self.cfg["tgt_lang"][:2] in ("zh", "ja"):
                # 機翻會在佔位符兩側塞空白，中日文不需要
                t = re.sub(r"[ 　]*(\[\[\s*\d+\s*\]\])[ 　]*", r"\1", t)
            out[k] = unmask(t, codes)
        return out, msgs

    def translate(self, data, out_path):
        """data：ManualTransFile.json 的內容。out_path 已存在就續翻。回傳最終結果 dict。"""
        cfg = self.cfg
        keys = list(data)
        result = {k: k for k in keys}
        if os.path.exists(out_path):
            old = load_json(out_path)
            for k in keys:
                v = old.get(k)
                if isinstance(v, str) and v and v != k:
                    result[k] = v
        seq = [k for k in keys if needs_translation(k, cfg["src_lang"])]
        pos = {k: i for i, k in enumerate(seq)}
        todo = [k for k in seq if result[k] == k]
        skipped = len(keys) - len(seq)
        total, base = len(seq), len(seq) - len(todo)
        done, failed, t0, last_save = base, 0, time.time(), time.time()
        if base:
            self.on_log(f"從既有輸出續翻：已完成 {base} 條，剩 {len(todo)} 條")
        self.on_log(f"共 {len(keys)} 條，需翻譯 {total} 條，跳過 {skipped} 條（無需翻譯的符號、數字等）")
        self._emit("翻譯", done, total, base, t0, skipped, failed)

        n_ctx = int(cfg.get("context_lines", 0))

        def work(batch):
            p = pos[batch[0]]
            ctx = [(s, result[s] if result[s] != s else "") for s in seq[max(0, p - n_ctx):p]]
            return self._translate_batch(batch, ctx)

        def on_result(batch, res):
            nonlocal done, failed, last_save
            if isinstance(res, Exception):
                self.on_log(f"整批失敗（{len(batch)} 條，保留原文）：{res}")
                got, msgs = {}, []
            else:
                got, msgs = res
            result.update(got)
            done += len(batch)
            failed += len(batch) - len(got)
            for m in msgs:
                self.on_log(m)
            if time.time() - last_save > 3:
                save_json(out_path, result)
                last_save = time.time()
            self._emit("翻譯", done, total, base, t0, skipped, failed)

        try:
            self._run_pool(list(self._batches(todo, int(cfg.get("batch_size", 20)))), work, on_result)
        finally:
            save_json(out_path, result)
        return result
