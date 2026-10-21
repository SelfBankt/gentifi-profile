"""Telegram adapter for the owner relay, using the Bot API with long polling.

Long polling means the bot asks Telegram for new messages, so it runs on any
machine with internet access: no public address, webhook or certificate needed.
Standard library only. Each thread keeps one connection open to Telegram, over IPv4 where
there is one: on the Gentifi VPS that's a 13 ms round trip against 200 ms over IPv6, and a new
secure connection costs several round trips, so owners' taps were answered seconds late.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .channel import Incoming, Keyboard

API = "https://api.telegram.org"
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}  # what the drafting model can read


class TelegramError(RuntimeError):
    pass


def _connect_ipv4_first(address: tuple[str, int], timeout: float | None = None,
                        source_address: tuple[str, int] | None = None) -> socket.socket:
    """socket.create_connection, trying IPv4 addresses before IPv6 ones."""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda info: info[0] != socket.AF_INET)
    error: OSError | None = None
    for family, kind, proto, _, addr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error or OSError(f"couldn't resolve {host}")


def _open(scheme: str, host: str, port: int | None) -> http.client.HTTPConnection:
    conn = (http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection)(host, port)
    conn._create_connection = _connect_ipv4_first  # set per connection by http.client, so after it
    return conn


# A reused connection Telegram has since closed fails like this before anything is sent
STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest, BrokenPipeError, ConnectionResetError)


class TelegramChannel:
    def __init__(self, token: str, api: str = API):
        if not token or ":" not in token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is missing or doesn't look like a bot token")
        self._files = f"{api}/file/bot{token}/"
        parts = urlsplit(api)
        self._server = (parts.scheme, parts.hostname or "", parts.port)
        self._path = f"{parts.path.rstrip('/')}/bot{token}/"
        self._local = threading.local()  # the poller and the relay each keep their own connection
        self._offset = 0
        self.username: str | None = None

    # ------------------------------------------------------------ API calls

    def _connection(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            scheme, host, port = self._server
            conn = _open(scheme, host, port)
            self._local.conn = conn
        return conn

    def _post(self, method: str, body: bytes, http_timeout: float) -> tuple[int, bytes]:
        for attempt in (1, 2):
            conn = self._connection()
            reused = conn.sock is not None
            conn.timeout = http_timeout
            if reused:
                conn.sock.settimeout(http_timeout)
            try:
                conn.request("POST", self._path + method, body=body,
                             headers={"Content-Type": "application/json", "Connection": "keep-alive"})
                resp = conn.getresponse()
                return resp.status, resp.read()
            except STALE:
                conn.close()
                if not reused or attempt == 2:
                    raise
            except BaseException:
                conn.close()  # a half-read response can't be reused
                raise
        raise AssertionError("unreachable")

    def _call(self, method: str, http_timeout: float = 30, **params: Any) -> Any:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        try:
            status, raw = self._post(method, body, http_timeout)
        except http.client.HTTPException as exc:
            # Never echo the URL: it contains the token.
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TelegramError(f"{method}: HTTP {status}") from None
        if not data.get("ok"):
            # Never echo the URL: it contains the token.
            raise TelegramError(f"{method}: {data.get('description', 'failed')}")
        return data["result"]

    def check(self) -> str:
        """Confirm the token works; returns the bot's @username."""
        me = self._call("getMe", http_timeout=15)
        self.username = me.get("username")
        return self.username or "?"

    @staticmethod
    def _markup(buttons: Keyboard | None) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": b.label, "callback_data": b.data} for b in row]
                                    for row in (buttons or [])]}

    # ------------------------------------------------------------ OwnerChannel

    def send(self, chat_id: str, text: str, buttons: Keyboard | None = None) -> str:
        msg = self._call("sendMessage", chat_id=chat_id, text=text,
                         reply_markup=self._markup(buttons) if buttons else None)
        return str(msg["message_id"])

    def edit(self, chat_id: str, message_id: str, text: str, buttons: Keyboard | None = None) -> None:
        try:
            self._call("editMessageText", chat_id=chat_id, message_id=int(message_id), text=text,
                       reply_markup=self._markup(buttons))
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise

    def send_photo(self, chat_id: str, photo: str, caption: str = "") -> str:
        """Send a photo the bot has already received, by its Telegram file id (nothing is re-uploaded)."""
        msg = self._call("sendPhoto", chat_id=chat_id, photo=photo, caption=caption[:1024] or None)
        return str(msg["message_id"])

    def download(self, file_id: str) -> bytes:
        """A file an owner sent (Telegram lets bots fetch up to 20 MB)."""
        path = self._call("getFile", file_id=file_id)["file_path"]
        try:
            with urllib.request.urlopen(self._files + path, timeout=60) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            # Never echo the URL: it contains the token.
            raise TelegramError(f"getFile: download failed ({getattr(exc, 'code', None) or exc.reason})") from None

    def poll(self, timeout: int) -> list[Incoming]:
        try:
            updates = self._call("getUpdates", http_timeout=timeout + 10, offset=self._offset,
                                 allowed_updates=["message", "callback_query"], timeout=timeout)
        except (TelegramError, OSError) as exc:
            print(f"gentifi-relay: Telegram poll failed ({exc}); retrying", file=sys.stderr)
            time.sleep(5)
            return []
        out = []
        for u in updates:
            self._offset = max(self._offset, u["update_id"] + 1)
            if "callback_query" in u:
                q = u["callback_query"]
                msg = q.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if chat is not None:
                    out.append(Incoming("button", str(chat), q.get("data", ""), str(msg.get("message_id")),
                                        ack_id=q["id"], name=q.get("from", {}).get("first_name"), text=msg.get("text")))
            elif "message" in u:
                m = u["message"]
                if m.get("chat", {}).get("type") != "private":
                    continue  # owners talk to the bot one to one
                chat, name = str(m["chat"]["id"]), m.get("from", {}).get("first_name")
                document = m.get("document") or {}
                if "text" in m:
                    out.append(Incoming("text", chat, m["text"], name=name))
                elif m.get("photo"):  # sizes, smallest first: take the largest
                    out.append(Incoming("photo", chat, m["photo"][-1]["file_id"], name=name))
                elif document.get("mime_type") in PHOTO_TYPES:  # a photo sent as a file, uncompressed
                    out.append(Incoming("photo", chat, document["file_id"], name=name,
                                        mime=document["mime_type"]))
                elif document:
                    out.append(Incoming("file", chat, document.get("mime_type", ""), name=name))
        return out

    def ack(self, incoming: Incoming, note: str | None = None) -> None:
        if incoming.ack_id:
            try:
                self._call("answerCallbackQuery", http_timeout=10, callback_query_id=incoming.ack_id, text=note)
            except (TelegramError, OSError):
                pass  # cosmetic only
