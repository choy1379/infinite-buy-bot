import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from laoer.notify import DiscordNotifier, KakaoNotifier, KakaoTokenStore, Message, chunk_text


class FakeResp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RecordingOpener:
    def __init__(self, responder):
        self.requests = []
        self.responder = responder

    def open(self, req, timeout=None):
        self.requests.append(req)
        return self.responder(req)


class ChunkTest(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(chunk_text("a\nb"), ["a\nb"])

    def test_long_text_numbered_and_within_limit(self):
        text = "\n".join(f"줄 {i} " + "가" * 40 for i in range(12))
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertTrue(chunks[0].startswith(f"(1/{len(chunks)}) "))


class DiscordTest(unittest.TestCase):
    def test_payload_and_send(self):
        opener = RecordingOpener(lambda req: FakeResp(204))
        DiscordNotifier("https://discord.example/hook", opener=opener).send(Message("제목", ["한 줄"], "success"))
        req = opener.requests[0]
        body = json.loads(req.data)
        self.assertEqual(body["embeds"][0]["title"], "제목")
        self.assertEqual(body["embeds"][0]["description"], "한 줄")
        self.assertIn("tecl-infinite-buy", req.get_header("User-agent"))


class KakaoTest(unittest.TestCase):
    def test_refreshes_expired_token_then_sends(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "kakao.json"
            KakaoTokenStore(path).save({"access_token": "old", "refresh_token": "r1", "expires_at": 0})

            def respond(req):
                if req.full_url.endswith("/oauth/token"):
                    form = urllib.parse.parse_qs(req.data.decode())
                    self.assertEqual(form["grant_type"], ["refresh_token"])
                    return FakeResp(200, json.dumps({"access_token": "new", "expires_in": 21599}).encode())
                self.assertEqual(req.get_header("Authorization"), "Bearer new")
                return FakeResp(200, b'{"result_code":0}')

            opener = RecordingOpener(respond)
            KakaoNotifier("key", path, opener=opener, clock=lambda: 1000.0).send(Message("t", ["x"]))
            send_req = opener.requests[-1]
            form = urllib.parse.parse_qs(send_req.data.decode())
            template = json.loads(form["template_object"][0])
            self.assertEqual(template["object_type"], "text")
            self.assertEqual(template["text"], "t\nx")
            saved = KakaoTokenStore(path).load()
            self.assertEqual(saved["access_token"], "new")
            self.assertEqual(saved["refresh_token"], "r1")


if __name__ == "__main__":
    unittest.main()
