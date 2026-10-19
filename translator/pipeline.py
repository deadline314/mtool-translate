"""翻譯流程：批次切分、執行緒池、重試退避、停止、斷路器、定期存檔、續翻、術語擷取。

錯誤處理原則：
- TransientError：等一下重試（遵守 Retry-After，等待中按停止會立刻結束）；重試用完就把這批記為失敗
- RequestError：把批次對半拆開重送，拆到單條仍失敗才記為失敗
- FatalError：中止整個工作
- 連續多批全部失敗、或連續多次請求都被拒絕：判定設定有問題，中止，避免白白浪費額度
- 無論怎麼結束都會存檔；輸出檔寫不進去時另存備份檔，翻譯結果不會遺失
"""
import json
import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .engines import ChatEngine, LineEngine
from .errors import Cancelled, FatalError, RateLimitError, RequestError, TranslatorError, TransientError
from .formats import MTOOL_JSON
from .text import (LANGS, mask, needs_translation, normalize_glossary, parse_json_reply, placeholder_error,
                   relevant_terms, strip_placeholder_spaces, term_error, unmask)

log = logging.getLogger(__name__)

PHASE_TRANSLATE = "翻譯"
PHASE_TERMS = "擷取術語"

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

MAX_TERM_LEN = 30


def _bg(background):
    return f"\n\n遊戲背景（供參考）：\n{background.strip()}" if background and background.strip() else ""


def _short(text, n=30):
    text = text.replace("\n", "⏎")
    return text if len(text) <= n else text[:n] + "…"


def _clamp(v, lo, hi, default):
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return default


@dataclass
class JobConfig:
    src_lang: str = "ja"
    tgt_lang: str = "zh-TW"
    batch_size: int = 20
    workers: int = 1
    context_lines: int = 5
    background: str = ""
    max_batch_chars: int = 3000      # 一批原文的字數上限，避免超出模型長度
    max_context_chars: int = 1500    # 前文參考的字數上限
    retries: int = 3                 # 暫時性錯誤的重試次數
    max_failed_batches: int = 5      # 連續幾批全部失敗就中止
    max_request_errors: int = 12     # 連續幾次請求被拒絕就中止
    save_interval: float = 3.0       # 最短存檔間隔（秒）；檔案大時會自動拉長

    def __post_init__(self):
        if self.src_lang not in LANGS or self.tgt_lang not in LANGS:
            raise FatalError(f"不支援的語言：{self.src_lang} → {self.tgt_lang}")
        self.batch_size = _clamp(self.batch_size, 1, 100, 20)
        self.workers = _clamp(self.workers, 1, 32, 1)
        self.context_lines = _clamp(self.context_lines, 0, 30, 5)


@dataclass
class Progress:
    phase: str
    done: int
    total: int
    skipped: int = 0
    failed: int = 0
    elapsed: float = 0.0
    rate: float = 0.0
    eta: float = None

    @property
    def percent(self):
        return 100.0 if self.total == 0 else self.done / self.total * 100


@dataclass
class BatchResult:
    out: dict = field(default_factory=dict)      # 原文 → 譯文
    failed: dict = field(default_factory=dict)   # 原文 → 失敗原因（保留原文）
    warnings: list = field(default_factory=list)
    error: Exception = None                      # 這批因暫時性錯誤沒做完
    # 沒出現在 out / failed 的條目 = 停止時還沒處理，下次續翻


class _Saver:
    """定期存檔。檔案越大存一次越久，間隔自動拉長，避免存檔拖慢翻譯。寫入失敗不會中斷翻譯。"""

    def __init__(self, fmt, path, on_log, interval):
        self.fmt, self.path, self.on_log, self.interval = fmt, path, on_log, interval
        self.last, self.cost, self.warned = time.monotonic(), 0.0, False

    def _save(self, path, data):
        t0 = time.monotonic()
        try:
            self.fmt.save(path, data)
        except OSError as e:
            if not self.warned:
                self.on_log(f"輸出檔暫時無法寫入（{e}），稍後會自動再試")
                self.warned = True
            log.warning("save failed: %s", e)
            return False
        finally:
            self.last = time.monotonic()
        self.cost, self.warned = self.last - t0, False
        return True

    def maybe(self, data):
        if time.monotonic() - self.last >= max(self.interval, self.cost * 5):
            self._save(self.path, data)

    def final(self, data):
        """最後一定要存到某個地方。回傳實際存檔的路徑。"""
        if self._save(self.path, data):
            return self.path
        root, ext = os.path.splitext(self.path)
        backup = f"{root}.recovered{ext or '.json'}"
        if self._save(backup, data):
            self.on_log(f"輸出檔無法寫入，結果已另存到：{backup}")
            return backup
        raise FatalError(f"輸出檔與備份檔都無法寫入，請檢查資料夾權限或磁碟空間：{self.path}")


class Job:
    """一次翻譯或術語擷取工作。on_progress(Progress)、on_log(str) 會從背景執行緒呼叫。"""

    def __init__(self, engine, config, glossary=None, on_progress=None, on_log=None):
        self.engine, self.cfg = engine, config
        self.glossary = normalize_glossary(glossary or {})
        self.on_progress = on_progress or (lambda p: None)
        self.on_log = on_log or (lambda m: None)
        self.halt = threading.Event()
        self.user_stopped = False
        self.found_terms = {}
        self._lock = threading.Lock()
        self._request_errors = 0

    def stop(self):
        """使用者按停止：不再送新請求，進行中的請求結束（或重試等待中）就收工並存檔。"""
        self.user_stopped = True
        self.halt.set()

    # ------------------------------------------------------------ 共用

    def _call(self, fn, *args):
        """呼叫引擎：暫時性錯誤退避重試；連續被拒絕太多次就判定設定有問題。"""
        cfg = self.cfg
        for attempt in range(cfg.retries + 1):
            if self.halt.is_set():
                raise Cancelled("已停止")
            try:
                r = fn(*args)
            except TransientError as e:
                if attempt == cfg.retries:
                    if e.rate_limited:
                        raise RateLimitError(f"{e}\n重試 {cfg.retries} 次仍被限流，請稍後再續翻，或調低「同時請求數」。") from e
                    raise
                delay = e.retry_after if e.retry_after is not None else min(2 ** (attempt + 1), 30)
                self.on_log(f"{e}；{delay:.0f} 秒後重試（{attempt + 1}/{cfg.retries}）")
                if self.halt.wait(delay):
                    raise Cancelled("已停止")
            except RequestError as e:
                with self._lock:
                    self._request_errors += 1
                    n = self._request_errors
                if n >= cfg.max_request_errors:
                    raise FatalError(f"連續 {n} 次請求都被拒絕，可能是模型或參數設定不對。最後一次錯誤：{e}") from e
                raise
            else:
                with self._lock:
                    self._request_errors = 0
                return r

    def _emit(self, phase, done, total, base, t0, skipped=0, failed=0):
        elapsed = time.monotonic() - t0
        new = done - base
        rate = new / elapsed if elapsed > 0 and new > 0 else 0.0
        eta = (total - done) / rate if rate else None
        self.on_progress(Progress(phase, done, total, skipped, failed, elapsed, rate, eta))

    def _run_pool(self, items, fn, on_result):
        """
        items 依序送進執行緒池，同時在跑的最多 workers 個（邊做邊送：記憶體固定、
        後面的批次能參考前面已完成的譯文、按停止時不會有一大堆排隊中的請求）。
        on_result(item, 結果或例外) 回傳錯誤訊息；連續多批都有錯就中止。
        """
        workers = self.cfg.workers
        it = iter(items)
        pending, fatal, streak = {}, None, 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            def fill():
                while len(pending) < workers and not self.halt.is_set():
                    item = next(it, None)
                    if item is None:
                        return
                    pending[pool.submit(fn, item)] = item

            fill()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    item = pending.pop(fut)
                    try:
                        res = fut.result()
                    except Cancelled:
                        continue
                    except FatalError as e:
                        fatal = fatal or e
                        self.halt.set()
                        continue
                    except Exception as e:  # 暫時性錯誤重試用完，或程式錯誤
                        if not isinstance(e, TranslatorError):
                            log.exception("worker crashed")
                            e = TransientError(f"程式錯誤：{type(e).__name__}: {e}")
                        res = e
                    err = on_result(item, res)
                    streak = streak + 1 if err else 0
                    if streak >= self.cfg.max_failed_batches and not fatal:
                        fatal = FatalError(f"連續 {streak} 批都失敗，已中止。最後的錯誤：{err}")
                        self.halt.set()
                fill()
        if fatal:
            raise fatal

    def _batches(self, items, size):
        batch, chars = [], 0
        for k in items:
            if batch and (len(batch) >= size or chars + len(k) > self.cfg.max_batch_chars):
                yield batch
                batch, chars = [], 0
            batch.append(k)
            chars += len(k)
        if batch:
            yield batch

    def _system(self, template):
        cfg = self.cfg
        return template.format(src=LANGS[cfg.src_lang], tgt=LANGS[cfg.tgt_lang], background=_bg(cfg.background))

    # ------------------------------------------------------------ 術語擷取

    def extract_terms(self, entries):
        """掃過全文找專有名詞。回傳 {原文: 譯名}（只含術語表裡還沒有的）；中止時已找到的在 self.found_terms。"""
        if not isinstance(self.engine, ChatEngine):
            raise FatalError("自動擷取術語需要對話型 LLM（雲端 API 或本地 LLM）")
        lines = [k for k in entries if needs_translation(k, self.cfg.src_lang)]
        chunks = list(self._batches(lines, 10 ** 9))  # 只依字數切
        system = self._system(TERMS_SYSTEM)
        found = self.found_terms
        t0, done = time.monotonic(), 0
        self.on_log(f"術語擷取：{len(lines)} 條文本分成 {len(chunks)} 段送出")
        self._emit(PHASE_TERMS, 0, len(chunks), 0, t0)

        def work(chunk):
            with self._lock:
                known = relevant_terms({**self.glossary, **found}, chunk)
            user = ""
            if known:
                user += "【已知術語】\n" + "\n".join(f"{k} => {v}" for k, v in known.items()) + "\n\n"
            user += "【文本】\n" + "\n".join(mask(k)[0] for k in chunk)
            try:
                return normalize_glossary(parse_json_reply(self._call(self.engine.complete, system, user)))
            except (ValueError, json.JSONDecodeError) as e:
                raise TransientError(f"回應無法解析：{e}") from e

        def on_result(chunk, res):
            nonlocal done
            done += 1
            err = ""
            if isinstance(res, Exception):
                err = str(res)
                self.on_log(f"術語擷取失敗（第 {done} 段）：{err}")
            else:
                blob = "\n".join(chunk)
                with self._lock:
                    for k, v in res.items():
                        # 只收原文真的出現過的詞，避免模型自己編
                        if len(k) <= MAX_TERM_LEN and k in blob and k not in self.glossary and k not in found:
                            found[k] = v
            self._emit(PHASE_TERMS, done, len(chunks), 0, t0)
            return err

        self._run_pool(chunks, work, on_result)
        return dict(found)

    # ------------------------------------------------------------ 翻譯：LLM

    def _chat_items(self, items, context, note=""):
        """送一批給 LLM。回傳 ({原文: 譯文}, {原文: (是否只是術語問題, 錯誤訊息, 候選譯文)})。"""
        masked = {str(i + 1): mask(k) for i, k in enumerate(items)}
        terms = relevant_terms(self.glossary, items + [s for s, _ in context])
        user = ""
        if terms:
            user += "【術語表】\n" + "\n".join(f"{k} => {v}" for k, v in terms.items()) + "\n\n"
        if context:
            user += "【前文】\n" + "\n".join(f"{mask(s)[0]} => {mask(t)[0]}" if t else mask(s)[0]
                                            for s, t in context) + "\n\n"
        if note:
            user += f"【注意】上一次翻譯有問題：{note}\n\n"
        user += "【待翻譯】\n" + json.dumps({i: m for i, (m, _) in masked.items()}, ensure_ascii=False, indent=0)

        reply_text = self._call(self.engine.complete, self._system(TRANSLATE_SYSTEM), user)
        try:
            reply = parse_json_reply(reply_text)
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

    def _translate_chat(self, batch, context, res):
        if self.halt.is_set():
            return
        try:
            out, errs = self._chat_items(batch, context)
        except RequestError as e:
            if len(batch) == 1:
                res.failed[batch[0]] = str(e)
                return
            mid = len(batch) // 2  # 太長、被拒絕：拆一半重送，把有問題的那條隔離出來
            self._translate_chat(batch[:mid], context, res)
            self._translate_chat(batch[mid:], context, res)
            return
        res.out.update(out)
        for k, (soft, err, cand) in errs.items():
            if self.halt.is_set():
                return
            try:
                o2, e2 = self._chat_items([k], context, note=err)
            except RequestError as e:
                o2, e2 = {}, {k: (False, str(e), None)}
            if k in o2:
                res.out[k] = o2[k]
            elif e2[k][0]:          # 重試後還是只有術語問題：收下，但留警告
                res.out[k] = e2[k][2]
                res.warnings.append(f"術語未套用：{_short(k)} ｜ {e2[k][1]}")
            elif soft:              # 重試反而更糟：用第一次的版本
                res.out[k] = cand
                res.warnings.append(f"術語未套用：{_short(k)} ｜ {err}")
            else:
                res.failed[k] = e2[k][1]

    # ------------------------------------------------------------ 翻譯：自帶提示格式的模型（Sakura）

    def _translate_lines(self, batch, res):
        if self.halt.is_set():
            return
        masked = [mask(k) for k in batch]
        terms = relevant_terms(self.glossary, batch)
        try:
            outs = self._call(self.engine.translate_lines, [m for m, _ in masked], terms)
        except RequestError as e:
            if len(batch) == 1:
                res.failed[batch[0]] = str(e)
                return
            mid = len(batch) // 2
            self._translate_lines(batch[:mid], res)
            self._translate_lines(batch[mid:], res)
            return
        cjk = self.cfg.tgt_lang[:2] in ("zh", "ja")
        for k, (m, codes), t in zip(batch, masked, outs):
            err = placeholder_error(m, t)
            if err:
                res.failed[k] = err
                continue
            final = unmask(strip_placeholder_spaces(t) if cjk else t, codes)
            res.out[k] = final
            err = term_error(k, final, terms)
            if err:
                res.warnings.append(f"術語未套用：{_short(k)} ｜ {err}")

    # ------------------------------------------------------------ 翻譯：機翻

    def _translate_mt(self, batch, res):
        cjk = self.cfg.tgt_lang[:2] in ("zh", "ja")
        for k in batch:
            if self.halt.is_set():
                return
            m, codes = mask(k, relevant_terms(self.glossary, [k]))
            try:
                t = self._call(self.engine.translate, m)
            except RequestError as e:
                res.failed[k] = str(e)
                continue
            err = placeholder_error(m, t)
            if err:
                res.failed[k] = err
                continue
            res.out[k] = unmask(strip_placeholder_spaces(t) if cjk else t, codes)

    def _translate_batch(self, batch, context):
        res = BatchResult()
        try:
            if isinstance(self.engine, ChatEngine):
                self._translate_chat(batch, context, res)
            elif isinstance(self.engine, LineEngine):
                self._translate_lines(batch, res)
            else:
                self._translate_mt(batch, res)
        except Cancelled:
            pass
        except FatalError:
            raise
        except Exception as e:  # 暫時性錯誤重試用完、或程式錯誤：已完成的保留，剩下的記為失敗
            if not isinstance(e, TranslatorError):
                log.exception("batch crashed")
                e = TransientError(f"程式錯誤：{type(e).__name__}: {e}")
            res.error = e
            for k in batch:
                if k not in res.out and k not in res.failed:
                    res.failed[k] = str(e)
        return res

    def _context(self, seq, pos, result):
        """前面幾句的原文與（已完成的）譯文，受行數與字數上限限制。"""
        ctx, chars = [], 0
        for s in reversed(seq[max(0, pos - self.cfg.context_lines):pos]):
            t = result.get(s, s)
            t = "" if t == s else t
            chars += len(s) + len(t)
            if ctx and chars > self.cfg.max_context_chars:
                break
            ctx.append((s, t))
        ctx.reverse()
        return ctx

    def translate(self, entries, out_path, fmt=MTOOL_JSON):
        """
        entries：原文清單。out_path 已存在就續翻（已翻好的不重送，檔案裡其他條目保留）。
        回傳 dict(saved_to, total, done, failed, skipped, stopped)。
        """
        cfg = self.cfg
        existing = fmt.load_existing(out_path)
        result = {}
        for k in entries:
            v = existing.get(k)
            result[k] = v if v and v != k else k
        for k, v in existing.items():  # 輸出檔裡有、這次原文沒有的條目照樣保留
            result.setdefault(k, v)

        seq = [k for k in entries if needs_translation(k, cfg.src_lang)]
        pos = {k: i for i, k in enumerate(seq)}
        todo = [k for k in seq if result[k] == k]
        skipped = len(entries) - len(seq)
        total, base = len(seq), len(seq) - len(todo)
        stats = dict(done=base, failed=0)
        t0 = time.monotonic()
        saver = _Saver(fmt, out_path, self.on_log, cfg.save_interval)
        if base:
            self.on_log(f"從既有輸出續翻：已完成 {base:,} 條，剩 {len(todo):,} 條")
        self.on_log(f"共 {len(entries):,} 條，需翻譯 {total:,} 條，跳過 {skipped:,} 條（數字、符號等不需翻譯）")
        self._emit(PHASE_TRANSLATE, base, total, base, t0, skipped, 0)

        def work(batch):
            return self._translate_batch(batch, self._context(seq, pos[batch[0]], result))

        def on_result(batch, res):
            if isinstance(res, Exception):
                res = BatchResult(failed={k: str(res) for k in batch}, error=res)
            result.update(res.out)
            stats["done"] += len(res.out) + len(res.failed)
            stats["failed"] += len(res.failed)
            for w in res.warnings:
                self.on_log(w)
            if res.error:
                self.on_log(f"這批有 {len(res.failed)} 條沒翻完（保留原文）：{res.error}")
            else:
                for k, why in res.failed.items():
                    self.on_log(f"翻譯失敗，保留原文：{_short(k)} ｜ {why}")
            saver.maybe(result)
            self._emit(PHASE_TRANSLATE, stats["done"], total, base, t0, skipped, stats["failed"])
            if res.error:
                return str(res.error)
            if res.failed and not res.out:
                return next(iter(res.failed.values()))
            return ""

        try:
            self._run_pool(self._batches(todo, cfg.batch_size), work, on_result)
        finally:
            saved_to = saver.final(result)
        return dict(saved_to=saved_to, total=total, done=stats["done"], failed=stats["failed"],
                    skipped=skipped, stopped=self.user_stopped)
