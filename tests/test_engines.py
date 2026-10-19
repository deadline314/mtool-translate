"""用本機假伺服器模擬 OpenAI 相容 / Ollama / Sakura / Claude / MyMemory，驗證引擎真的走 HTTP 時的行為。"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from translator import formats
from translator.engines import (PROVIDER_BY_ID, ClaudeEngine, GoogleFreeEngine, MyMemoryEngine, OllamaEngine,
                                OpenAICompatEngine, SakuraEngine, make_engine)
from translator.errors import FatalError, RateLimitError, RequestError, TransientError
from translator.pipeline import Job, JobConfig

DICT = {"はい": "是", "いいえ": "否", "アリス": "愛麗絲", "が来た": "來了", "こんにちは": "你好"}


def tr(s):
    for a, b in DICT.items():
        s = s.replace(a, b)
    return s


def chat_reply(system, user):
    if system.startswith("你是一个轻小说"):  # Sakura 一行一句
        text = user.split("翻译成中文：", 1)[1]
        return "\n".join(tr(line) for line in text.split("\n"))
    if "【文本】" in user:  # 術語擷取
        return json.dumps({"アリス": "愛麗絲", "原文沒有的詞": "X"}, ensure_ascii=False)
    if "【待翻譯】" in user:
        todo = json.loads(user.split("【待翻譯】\n", 1)[1])
        return json.dumps({k: tr(v) for k, v in todo.items()}, ensure_ascii=False)
    return tr(user)


class Handler(BaseHTTPRequestHandler):
    log_message = lambda *a: None  # noqa: E731
    state = {}

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/v1/models" and self.headers.get("x-api-key"):
            return self._send(200, {"data": [{"id": "claude-haiku-5-5", "type": "model", "display_name": "Haiku",
                                              "created_at": "2026-01-01T00:00:00Z"}],
                                    "has_more": False, "first_id": "claude-haiku-5-5", "last_id": "claude-haiku-5-5"})
        if u.path == "/v1/models":
            return self._send(200, {"data": [{"id": "b-model"}, {"id": "a-model"}]})
        if u.path == "/api/tags":
            return self._send(200, {"models": [{"name": "qwen2.5:14b"}]})
        if u.path == "/get":  # MyMemory
            q = urllib.parse.parse_qs(u.query)
            text = q["q"][0]
            Handler.state.setdefault("mymemory", []).append(text)
            if "QUOTA" in text:
                return self._send(200, {"responseStatus": 429, "quotaFinished": True, "responseData": {}})
            return self._send(200, {"responseStatus": 200, "responseData": {"translatedText": tr(text) + "&#39;"}})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        body = self._body()
        Handler.state.setdefault("posts", []).append((u.path, dict(self.headers), body))
        if u.path == "/v1/chat/completions":
            model = body.get("model")
            if model == "auth":
                return self._send(401, {"error": {"message": "bad key"}})
            if model == "bad":
                return self._send(400, {"error": {"message": "unsupported parameter"}})
            if model == "quota":
                return self._send(429, {"error": {"code": "insufficient_quota"}})
            if model == "busy":
                return self._send(429, {"error": "slow down"}, {"Retry-After": "7"})
            if model == "down":
                return self._send(503, {"error": "overloaded"})
            if model == "length":
                return self._send(200, {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]})
            if model == "slow":
                time.sleep(0.15)
            msgs = body["messages"]
            content = chat_reply(msgs[0]["content"], msgs[1]["content"])
            if model == "thinker":
                content = "好，先想想 {格式}...</think>\n" + content
            return self._send(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
        if u.path == "/api/chat":
            msgs = body["messages"]
            return self._send(200, {"message": {"content": chat_reply(msgs[0]["content"], msgs[1]["content"])},
                                    "done_reason": "stop"})
        if u.path == "/v1/messages":
            if self.headers.get("x-api-key") == "bad":
                return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad"}})
            model = body["model"]
            stop = "refusal" if model == "claude-refuse" else "end_turn"
            text = chat_reply(body["system"], body["messages"][0]["content"])
            return self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                    "content": [{"type": "text", "text": text}], "stop_reason": stop,
                                    "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
        self._send(404, {"error": "not found"})


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Handler.state.clear()


class OpenAICompatTest(ServerTest):
    def eng(self, model, key=""):
        return OpenAICompatEngine(self.url + "/v1", key, model, temperature=0.2, timeout=5)

    def test_complete_and_models(self):
        self.assertEqual(self.eng("m", "sk-1").complete("翻譯", "こんにちは"), "你好")
        path, headers, body = Handler.state["posts"][-1]
        self.assertEqual(headers.get("Authorization"), "Bearer sk-1")
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(self.eng("m").list_models(), ["a-model", "b-model"])

    def test_error_classification(self):
        with self.assertRaises(FatalError):
            self.eng("auth").complete("s", "u")
        with self.assertRaises(RequestError):
            self.eng("bad").complete("s", "u")
        with self.assertRaises(RateLimitError):
            self.eng("quota").complete("s", "u")
        with self.assertRaises(TransientError) as cm:
            self.eng("busy").complete("s", "u")
        self.assertEqual((cm.exception.retry_after, cm.exception.rate_limited), (7.0, True))
        with self.assertRaises(TransientError):
            self.eng("down").complete("s", "u")
        with self.assertRaises(RequestError):
            self.eng("length").complete("s", "u")
        with self.assertRaises(FatalError):
            self.eng("").complete("s", "u")  # 沒填模型
        with self.assertRaises(FatalError):
            OpenAICompatEngine(self.url + "/nope", "", "m", timeout=5).complete("s", "u")  # 404

    def test_bad_url_and_refused_connection(self):
        with self.assertRaises(FatalError):
            OpenAICompatEngine("https://", "", "m")
        with self.assertRaises(FatalError):
            OpenAICompatEngine("localhost:1234", "", "m")
        with self.assertRaises(TransientError):
            OpenAICompatEngine("http://127.0.0.1:9/v1", "", "m", timeout=2).complete("s", "u")

    def test_end_to_end_translation_with_thinking_model(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            job = Job(self.eng("thinker"), JobConfig(batch_size=5), {"アリス": "愛麗絲"})
            job.translate(["\\C[2]アリス\\C[0]が来た", "はい", "いいえ", "100"], out)
            res = formats.read_json(out)
        self.assertEqual(res, {"\\C[2]アリス\\C[0]が来た": "\\C[2]愛麗絲\\C[0]來了", "はい": "是",
                               "いいえ": "否", "100": "100"})
        self.assertEqual(len(Handler.state["posts"]), 1)  # 思考過程裡的大括號不影響解析，一次就成功


class OllamaTest(ServerTest):
    def test_native_api_with_context_length(self):
        eng = make_engine(PROVIDER_BY_ID["ollama"], self.url + "/v1", "", "qwen2.5:14b")  # 舊設定帶 /v1 也能用
        self.assertIsInstance(eng, OllamaEngine)
        self.assertEqual(eng.complete("s", "こんにちは"), "你好")
        path, _, body = Handler.state["posts"][-1]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(body["options"]["num_ctx"], OllamaEngine.NUM_CTX)
        self.assertFalse(body["stream"])
        self.assertEqual(eng.list_models(), ["qwen2.5:14b"])


class SakuraTest(ServerTest):
    def test_line_protocol_and_glossary(self):
        eng = SakuraEngine(self.url + "/v1", timeout=5)
        out = eng.translate_lines(["[[0]]アリス[[1]]が来た", "はい", "1行目\n2行目はい"], {"アリス": "愛麗絲"})
        self.assertEqual(out, ["[[0]]愛麗絲[[1]]來了", "是", "1行目\n2行目是"])
        users = [b["messages"][1]["content"] for _, _, b in Handler.state["posts"]]
        self.assertEqual(len(users), 2)  # 單行的合併送、多行的單獨送
        self.assertIn("アリス->愛麗絲", users[0])

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.json")
            eng = make_engine(PROVIDER_BY_ID["sakura"], self.url + "/v1", "", "sakura")
            Job(eng, JobConfig(), {}).translate(["\\N[1]こんにちは", "いいえ"], out)
            self.assertEqual(formats.read_json(out), {"\\N[1]こんにちは": "\\N[1]你好", "いいえ": "否"})


class ClaudeTest(ServerTest):
    def eng(self, model, key="sk-ant-test"):
        return ClaudeEngine(key, model, base_url=self.url, timeout=5)

    def test_complete_with_fallback_param(self):
        self.assertEqual(self.eng("claude-opus-5-5").complete("翻譯", "こんにちは"), "你好")
        path, headers, body = Handler.state["posts"][-1]
        self.assertEqual(body["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", headers.get("anthropic-beta", ""))
        self.assertEqual(self.eng("claude-haiku-5-5").complete("翻譯", "はい"), "是")
        self.assertNotIn("fallbacks", Handler.state["posts"][-1][2])

    def test_errors_and_models(self):
        with self.assertRaises(RequestError):
            self.eng("claude-refuse").complete("s", "u")
        with self.assertRaises(FatalError):
            self.eng("claude-opus-5-5", key="bad").complete("s", "u")
        with self.assertRaises(TransientError):
            ClaudeEngine("k", "claude-opus-5-5", base_url="http://127.0.0.1:9", timeout=2).complete("s", "u")
        self.assertEqual(self.eng("x").list_models(), ["claude-haiku-5-5"])


class FreeServicesTest(ServerTest):
    def test_mymemory(self):
        with mock.patch.object(MyMemoryEngine, "URL", self.url + "/get"):
            eng = MyMemoryEngine("ja", "zh-TW", timeout=5)
            self.assertEqual(eng.translate("はい"), "是'")  # HTML 跳脫字元有還原
            long_line = "はい。" * 200
            eng.translate(long_line)
            self.assertTrue(all(len(q) <= MyMemoryEngine.MAX_CHARS for q in Handler.state["mymemory"]))
            with self.assertRaises(RateLimitError):
                eng.translate("QUOTA")

    def test_google_unreachable_is_transient(self):
        with mock.patch.object(GoogleFreeEngine, "URL", "http://127.0.0.1:9/x"):
            with self.assertRaises(TransientError):
                GoogleFreeEngine("ja", "zh-TW", timeout=2).translate("はい")


class FactoryTest(unittest.TestCase):
    def test_every_provider_builds(self):
        for p in PROVIDER_BY_ID.values():
            url = p.base_url if p.base_url.startswith("http") and p.base_url != "https://" else "http://x"
            self.assertIsNotNone(make_engine(p, url, "", p.model or "m"), p.id)


if __name__ == "__main__":
    unittest.main()
