"""Optional shared-password login (PUMPKIN_PASSWORD).

The browser's Basic-auth prompt is used once; after that a cookie authenticates,
which also covers WebSockets and the MJPEG <img>.
"""
import base64
import hashlib
import hmac

COOKIE = "pumpkin_auth"


class PasswordMiddleware:
    def __init__(self, app, password: str):
        self.app = app
        self.password = password
        self.token = hashlib.sha256(b"pumpkin:" + password.encode()).hexdigest()

    async def __call__(self, scope, receive, send):
        if not self.password or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if self._cookie_ok(headers.get(b"cookie", b"")):
            return await self.app(scope, receive, send)
        if self._basic_ok(headers.get(b"authorization", b"")):
            if scope["type"] == "websocket":
                return await self.app(scope, receive, send)
            cookie = f"{COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"

            async def send_with_cookie(msg):
                if msg["type"] == "http.response.start":
                    msg = {**msg, "headers": [*msg.get("headers", []), (b"set-cookie", cookie.encode())]}
                await send(msg)
            return await self.app(scope, receive, send_with_cookie)

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"www-authenticate", b'Basic realm="PumpkinHead"'),
                                (b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"login required"})

    def _cookie_ok(self, raw: bytes) -> bool:
        for part in raw.decode("latin-1").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, self.token):
                return True
        return False

    def _basic_ok(self, raw: bytes) -> bool:
        scheme, _, b64 = raw.decode("latin-1").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            _, _, pw = base64.b64decode(b64).decode().partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(pw.encode(), self.password.encode())
