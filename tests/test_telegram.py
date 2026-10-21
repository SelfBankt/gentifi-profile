"""Telegram adapter against a local fake Bot API (no network, no real token)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gentifi_profile.relay import Button
from gentifi_profile.relay.telegram import TelegramChannel, TelegramError

TOKEN = "123456:TEST-token"


@pytest.fixture
def api():
    calls = []
    updates = [
        {"update_id": 7, "message": {"message_id": 1, "chat": {"id": 1001, "type": "private"},
                                     "from": {"first_name": "Jo"}, "text": "/start abc"}},
        {"update_id": 8, "message": {"message_id": 2, "chat": {"id": -5, "type": "group"}, "text": "hi"}},
        {"update_id": 9, "callback_query": {"id": "cq1", "data": "a:GTF-1", "from": {"first_name": "Jo"},
                                            "message": {"message_id": 42, "chat": {"id": 1001}}}},
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            token, method = self.path.strip("/").split("/")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((method, body))
            if token != "bot" + TOKEN:
                out = {"ok": False, "description": "Unauthorized"}
            elif method == "getMe":
                out = {"ok": True, "result": {"username": "TestGentifiBot"}}
            elif method == "getUpdates":
                out = {"ok": True, "result": [u for u in updates if u["update_id"] >= body.get("offset", 0)]}
            elif method == "sendMessage":
                out = {"ok": True, "result": {"message_id": 99}}
            elif method == "editMessageText":
                out = {"ok": False, "description": "Bad Request: message is not modified"}
            else:
                out = {"ok": True, "result": True}
            data = json.dumps(out).encode()
            self.send_response(200 if out["ok"] else 400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()


def test_round_trip(api):
    url, calls = api
    ch = TelegramChannel(TOKEN, api=url)
    assert ch.check() == "TestGentifiBot"
    got = ch.poll(0)
    assert [(i.kind, i.chat_id, i.value) for i in got] == [("text", "1001", "/start abc"),
                                                          ("button", "1001", "a:GTF-1")]  # group ignored
    assert got[1].message_id == "42" and got[1].ack_id == "cq1"
    assert ch.poll(0) == []  # offset moved past what was seen
    assert ch.send("1001", "Hi", [[Button("Accept", "a:GTF-1")]]) == "99"
    method, body = calls[-1]
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Accept", "callback_data": "a:GTF-1"}]]}
    ch.edit("1001", "99", "Same")  # "not modified" is not an error
    ch.ack(got[1], "Accepted.")
    assert calls[-1] == ("answerCallbackQuery", {"callback_query_id": "cq1", "text": "Accepted."})


def test_bad_token_errors_never_show_the_token(api):
    url, _ = api
    with pytest.raises(TelegramError) as exc:
        TelegramChannel("999:wrong", api=url).check()
    assert "999:wrong" not in str(exc.value)
    with pytest.raises(TelegramError):
        TelegramChannel("", api=url)


def test_photos_are_received_downloaded_and_passed_on():
    calls = []
    updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 1001, "type": "private"}, "from": {"first_name": "Jo"},
                                     "photo": [{"file_id": "small"}, {"file_id": "large"}]}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 1001, "type": "private"},
                                     "document": {"file_id": "doc1", "mime_type": "image/png"}}},
        {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 1001, "type": "private"},
                                     "document": {"file_id": "doc2", "mime_type": "image/heic"}}},
        {"update_id": 4, "message": {"message_id": 4, "chat": {"id": -5, "type": "group"}, "photo": [{"file_id": "x"}]}},
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, kind="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            method = self.path.rsplit("/", 1)[1]
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((method, body))
            result = {"getUpdates": updates, "getFile": {"file_path": "photos/file_7.jpg"},
                      "sendPhoto": {"message_id": 55}}.get(method, True)
            self.reply(200, json.dumps({"ok": True, "result": result}).encode())

        def do_GET(self):
            calls.append(("GET", self.path))
            if self.path == f"/file/bot{TOKEN}/photos/file_7.jpg":
                self.reply(200, b"JPEG-BYTES", "image/jpeg")
            else:
                self.reply(404, b"")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ch = TelegramChannel(TOKEN, api=f"http://127.0.0.1:{server.server_port}")
        got = ch.poll(0)
        assert [(i.kind, i.value, i.mime) for i in got] == [
            ("photo", "large", "image/jpeg"), ("photo", "doc1", "image/png"), ("file", "image/heic", "image/jpeg")]
        assert got[0].name == "Jo"
        assert ch.download("large") == b"JPEG-BYTES"
        assert ("getFile", {"file_id": "large"}) in calls
        assert ch.send_photo("3003", "large", "Hollowbrook sent a photo.") == "55"
        assert ("sendPhoto", {"chat_id": "3003", "photo": "large", "caption": "Hollowbrook sent a photo."}) in calls

        bad = TelegramChannel(TOKEN.replace("TEST", "GONE"), api=f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(TelegramError) as exc:
            bad.download("large")
        assert "GONE" not in str(exc.value), "the token never appears in errors"
    finally:
        server.shutdown()
