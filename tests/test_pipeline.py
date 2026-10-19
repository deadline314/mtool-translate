import json
import logging
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from translator import formats
from translator.engines import ChatEngine, LineEngine, MTEngine
from translator.errors import FatalError, RateLimitError, RequestError, TransientError
from translator.pipeline import Job, JobConfig

logging.disable(logging.CRITICAL)  # 測試會故意製造錯誤，不需要印出日誌

DICT = {"はい": "是", "いいえ": "否", "アリス": "愛麗絲", "が来た": "來了", "テスト": "測試", "です": "是"}


def fake_translate(s):
    for a, b in DICT.items():
        s = s.replace(a, b)
    return s


class FakeLLM(ChatEngine):
    """照字典翻譯。可設定：某些字詞故意弄丟控制碼、某些字詞讓整批被拒絕、前幾次呼叫丟暫時性錯誤。"""

    def __init__(self, drop_codes=(), reject=(), transient=0, delay=0.0):
        self.calls, self.lock = [], threading.Lock()
        self.drop_codes, self.reject, self.transient, self.delay = drop_codes, reject, transient, delay

    def complete(self, system, user):
        with self.lock:
            self.calls.append(user)
            if self.transient:
                self.transient -= 1
                raise TransientError("伺服器忙", retry_after=0)
        if self.delay:
            time.sleep(self.delay)
        if "【文本】" in user:  # 術語擷取
            return '{"アリス": "愛麗絲", "不存在的詞": "X"}'
        todo = json.loads(user.split("【待翻譯】\n", 1)[1])
        if any(r in s for r in self.reject for s in todo.values()):
            raise RequestError("HTTP 400 內容被拒絕")
        out = {}
        for i, s in todo.items():
            t = fake_translate(s)
            if any(d in s for d in self.drop_codes):
                t = re.sub(r"\[\[\d+\]\]", "", t)
            out[i] = t
        return "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"


def cfg(**kw):
    base = dict(batch_size=2, workers=2, context_lines=2, retries=2)
    base.update(kw)
    return JobConfig(**base)


class Run:
    def __init__(self, engine, config=None, glossary=None):
        self.progress, self.logs = [], []
        self.job = Job(engine, config or cfg(), glossary, self.progress.append, self.logs.append)

    def translate(self, entries, out):
        return self.job.translate(entries, out)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.json")

    def tearDown(self):
        self.tmp.cleanup()

    def assert_progress_ok(self, progress):
        dones = [p.done for p in progress]
        self.assertEqual(dones, sorted(dones), "進度不能倒退")
        self.assertTrue(all(0 <= p.done <= p.total for p in progress))

    def test_full_translate_codes_skip_terms_context(self):
        entries = ["はい", "いいえ", "123", "\\C[2]アリス\\C[0]が来た", "\\V[1]壊れ", "はいはい"]
        eng = FakeLLM(drop_codes=["壊れ"])
        r = Run(eng, glossary={"アリス": "愛麗絲"})
        summary = r.translate(entries, self.out)
        res = formats.read_json(self.out)
        self.assertEqual(res["はい"], "是")
        self.assertEqual(res["123"], "123")
        self.assertEqual(res["\\C[2]アリス\\C[0]が来た"], "\\C[2]愛麗絲\\C[0]來了")
        self.assertEqual(res["\\V[1]壊れ"], "\\V[1]壊れ")  # 弄丟控制碼：重試後仍失敗 → 保留原文
        self.assertTrue(any("壊れ" in m and "遺漏控制碼" in m for m in r.logs), r.logs)
        self.assertTrue(any("アリス => 愛麗絲" in c for c in eng.calls))
        self.assertTrue(any("【前文】" in c for c in eng.calls))
        self.assert_progress_ok(r.progress)
        last = r.progress[-1]
        self.assertEqual((last.done, last.total, last.failed, last.skipped), (5, 5, 1, 1))
        self.assertEqual(summary["saved_to"], self.out)

    def test_context_includes_previous_translation_when_sequential(self):
        eng = FakeLLM()
        Run(eng, cfg(workers=1, batch_size=1)).translate(["はい", "いいえ"], self.out)
        self.assertIn("はい => 是", eng.calls[1])

    def test_resume_only_sends_remaining_and_keeps_extra_entries(self):
        formats.MTOOL_JSON.save(self.out, {"はい": "是", "別のゲーム": "別的遊戲"})
        eng = FakeLLM()
        r = Run(eng)
        r.translate(["はい", "いいえ"], self.out)
        res = formats.read_json(self.out)
        self.assertEqual(res, {"はい": "是", "いいえ": "否", "別のゲーム": "別的遊戲"})
        self.assertTrue(all("\"はい\"" not in c.split("【待翻譯】")[1] for c in eng.calls))
        self.assertEqual(r.progress[0].done, 1)
        self.assertEqual(r.progress[-1].done, 2)

    def test_corrupt_existing_output_is_not_overwritten(self):
        with open(self.out, "w", encoding="utf-8") as f:
            f.write("{壞掉")
        with self.assertRaises(FatalError):
            Run(FakeLLM()).translate(["はい"], self.out)
        with open(self.out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{壞掉")

    def test_request_error_splits_batch_and_isolates_bad_item(self):
        entries = ["はい", "いいえ", "だめ", "テスト"]
        r = Run(FakeLLM(reject=["だめ"]), cfg(batch_size=4, workers=1))
        r.translate(entries, self.out)
        res = formats.read_json(self.out)
        self.assertEqual((res["はい"], res["いいえ"], res["テスト"], res["だめ"]), ("是", "否", "測試", "だめ"))
        self.assertEqual(r.progress[-1].failed, 1)

    def test_transient_errors_are_retried(self):
        r = Run(FakeLLM(transient=2), cfg(workers=1))
        r.translate(["はい"], self.out)
        self.assertEqual(formats.read_json(self.out)["はい"], "是")
        self.assertEqual(sum("後重試" in m for m in r.logs), 2)

    def test_circuit_breaker_stops_when_every_batch_fails(self):
        class Down(ChatEngine):
            calls = 0

            def complete(self, s, u):
                Down.calls += 1
                raise TransientError("連線失敗", retry_after=0)
        entries = [f"テスト{i}" for i in range(100)]
        with self.assertRaises(FatalError) as cm:
            Run(Down(), cfg(workers=1, batch_size=1, retries=1, max_failed_batches=3)).translate(entries, self.out)
        self.assertIn("連續 3 批", str(cm.exception))
        self.assertLess(Down.calls, 10)
        self.assertTrue(os.path.exists(self.out))

    def test_rejected_every_request_aborts(self):
        class Rejects(ChatEngine):
            def complete(self, s, u):
                raise RequestError("HTTP 400 不支援的參數")
        with self.assertRaises(FatalError) as cm:
            Run(Rejects(), cfg(workers=1, batch_size=8, max_request_errors=5)).translate(
                [f"テスト{i}" for i in range(40)], self.out)
        self.assertIn("連續 5 次請求都被拒絕", str(cm.exception))

    def test_persistent_rate_limit_stops_job(self):
        class Limited(ChatEngine):
            def complete(self, s, u):
                raise TransientError("HTTP 429", retry_after=0, rate_limited=True)
        with self.assertRaises(RateLimitError):
            Run(Limited(), cfg(workers=1, retries=1)).translate(["はい"], self.out)

    def test_fatal_error_saves_progress(self):
        class Bad(ChatEngine):
            def complete(self, s, u):
                raise FatalError("HTTP 401")
        with self.assertRaises(FatalError):
            Run(Bad()).translate([f"はい{i}" for i in range(10)], self.out)
        self.assertTrue(os.path.exists(self.out))

    def test_stop_is_fast_and_not_counted_as_failure(self):
        r = Run(FakeLLM(delay=0.05), cfg(workers=1, batch_size=1))
        entries = [f"テスト{i}" for i in range(200)]
        threading.Timer(0.3, r.job.stop).start()
        t0 = time.monotonic()
        summary = r.translate(entries, self.out)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["failed"], 0)
        self.assertLess(summary["done"], 200)
        done_before = summary["done"]
        r2 = Run(FakeLLM())
        r2.translate(entries, self.out)
        self.assertEqual(r2.progress[0].done, done_before)
        self.assertEqual(r2.progress[-1].done, 200)

    def test_stop_during_retry_wait_returns_immediately(self):
        class Slow(ChatEngine):
            def complete(self, s, u):
                raise TransientError("忙", retry_after=60)
        r = Run(Slow(), cfg(workers=1))
        threading.Timer(0.2, r.job.stop).start()
        t0 = time.monotonic()
        r.translate(["はい"], self.out)
        self.assertLess(time.monotonic() - t0, 2)

    def test_save_failure_does_not_kill_job_and_falls_back_to_backup(self):
        real = formats.write_json_atomic

        def locked(path, data):
            if path == self.out:
                raise PermissionError("被其他程式鎖住")
            real(path, data)
        with mock.patch.object(formats, "write_json_atomic", locked):
            r = Run(FakeLLM(), cfg(save_interval=0))
            summary = r.translate(["はい", "いいえ", "テスト"], self.out)
        backup = os.path.join(self.tmp.name, "out.recovered.json")
        self.assertEqual(summary["saved_to"], backup)
        self.assertEqual(formats.read_json(backup)["いいえ"], "否")
        self.assertEqual(sum("暫時無法寫入" in m for m in r.logs), 1)

    def test_unexpected_exception_in_engine_is_contained(self):
        class Buggy(ChatEngine):
            def complete(self, s, u):
                raise KeyError("bug")
        with self.assertRaises(FatalError):  # 每批都壞 → 斷路器中止，而不是整個程式崩潰
            Run(Buggy(), cfg(workers=1, batch_size=1, max_failed_batches=2)).translate(["はい", "いいえ", "テスト"],
                                                                                       self.out)


class OtherEnginesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_mt_engine_terms_and_spaces(self):
        class FakeMT(MTEngine):
            def translate(self, text):
                return re.sub(r"(\[\[\d+\]\])", r" \1 ", fake_translate(text.replace("アリス", "艾莉絲")))
        Run(FakeMT(), glossary={"アリス": "愛麗絲"}).translate(["\\C[2]アリス\\C[0]が来た"], self.out)
        self.assertEqual(formats.read_json(self.out)["\\C[2]アリス\\C[0]が来た"], "\\C[2]愛麗絲\\C[0]來了")

    def test_mt_partial_batch_kept_on_transient_failure(self):
        class HalfDown(MTEngine):
            def translate(self, text):
                if "いいえ" in text:
                    raise TransientError("斷線", retry_after=0)
                return fake_translate(text)
        r = Run(HalfDown(), cfg(workers=1, batch_size=3, retries=0))
        r.translate(["はい", "いいえ", "テスト"], self.out)
        res = formats.read_json(self.out)
        self.assertEqual(res["はい"], "是")
        self.assertEqual(r.progress[-1].done, 3)
        self.assertEqual(r.progress[-1].failed, 2)

    def test_line_engine(self):
        class FakeLines(LineEngine):
            def __init__(self):
                self.terms = []

            def translate_lines(self, texts, terms):
                self.terms.append(terms)
                return [fake_translate(t) for t in texts]
        eng = FakeLines()
        Run(eng, glossary={"アリス": "愛麗絲"}).translate(["\\C[1]アリス\\C[0]が来た", "はい"], self.out)
        self.assertEqual(formats.read_json(self.out)["\\C[1]アリス\\C[0]が来た"], "\\C[1]愛麗絲\\C[0]來了")
        self.assertIn({"アリス": "愛麗絲"}, eng.terms)


class ExtractTermsTest(unittest.TestCase):
    def test_filters_invented_terms(self):
        job = Job(FakeLLM(), cfg(workers=1), {})
        self.assertEqual(job.extract_terms(["アリスが来た", "はい"]), {"アリス": "愛麗絲"})

    def test_partial_terms_kept_on_fatal(self):
        class Flaky(ChatEngine):
            n = 0

            def complete(self, s, u):
                Flaky.n += 1
                if Flaky.n > 1:
                    raise FatalError("HTTP 401")
                return '{"アリス": "愛麗絲"}'
        job = Job(Flaky(), cfg(workers=1, max_batch_chars=10), {})
        with self.assertRaises(FatalError):
            job.extract_terms(["アリスが来た", "ボブが来た", "カレンが来た"])
        self.assertEqual(job.found_terms, {"アリス": "愛麗絲"})

    def test_requires_chat_engine(self):
        class FakeMT(MTEngine):
            def translate(self, text):
                return text
        with self.assertRaises(FatalError):
            Job(FakeMT(), cfg(), {}).extract_terms(["はい"])


class ConfigTest(unittest.TestCase):
    def test_clamps(self):
        c = JobConfig(batch_size=0, workers=999, context_lines=-3)
        self.assertEqual((c.batch_size, c.workers, c.context_lines), (1, 32, 0))

    def test_bad_language(self):
        with self.assertRaises(FatalError):
            JobConfig(src_lang="xx")


if __name__ == "__main__":
    unittest.main()
