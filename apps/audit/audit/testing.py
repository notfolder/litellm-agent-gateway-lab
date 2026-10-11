"""試験用: 検索アプリのセッションの Cookie を作る（./lab test-audit が kubectl exec で使う）。

サインイン（OpenID Connect）を通らずに、利用者として API を呼ぶため。作れるのは、検索アプリと同じ署名の鍵
（AUDIT_SESSION_SECRET）を持つ人だけ（Pod の中か .env）。python -m audit.testing <email>
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from base64 import b64encode

from itsdangerous import TimestampSigner

from .viewer import role_of


def make_cookie(email: str) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    session = {"user": {"email": email.lower(), "role": role_of(email)}, "csrf": csrf}
    data = b64encode(json.dumps(session).encode())
    cookie = TimestampSigner(str(os.environ["AUDIT_SESSION_SECRET"])).sign(data).decode()
    return cookie, csrf


if __name__ == "__main__":
    c, t = make_cookie(sys.argv[1])
    print(json.dumps({"cookie": c, "csrf": t}))
