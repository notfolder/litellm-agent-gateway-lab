"""検索アプリの権限、閲覧の記録、CSRF の単体テスト（S3 の代わりに偽の Store を使う）。"""

import json
import os
import unittest
from base64 import b64encode

os.environ.update({"AUDIT_SESSION_SECRET": "unit-secret", "AUDIT_ADMINS": "admin@x", "AUDIT_AUDITORS": "aud@x"})

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from audit.viewer import create_app, role_of  # noqa: E402

ROW_A = {"call_id": "c1", "user_email": "a@x", "user_id": "a@x", "ts_start": "2026-10-11T01:00:00+00:00",
         "body_key": "raw/body/x.gz", "body_offset": 0, "body_length": 10}


class FakeStore:
    def __init__(self):
        self.views, self.fail_write = [], False
        self.saved = []

    def usage(self, d0, d1, email, group_by):
        rows = [{"user_email": "a@x", "cost_usd": 1.0, "requests": 1}, {"user_email": "b@x", "cost_usd": 2.0, "requests": 1}]
        return [r for r in rows if not email or r["user_email"] == email]

    def requests(self, t0, t1, email, filters, page):
        return [r for r in [ROW_A] if not email or r["user_email"] == email], False

    def request(self, call_id, at, email):
        return ROW_A if call_id == "c1" and (not email or email == ROW_A["user_email"]) else None

    def body(self, row):
        return {"payload": {"messages": [{"role": "user", "content": "secret"}]}}

    def write_access(self, kind, record):
        if self.fail_write:
            raise ConnectionError("down")
        self.views.append((kind, record))

    def read_access(self, kind, d0, d1):
        return [r for k, r in self.views if k == kind]

    def settlement(self, month, email):
        return [r for r in [{"user_email": "a@x"}, {"user_email": "b@x"}] if not email or r["user_email"] == email]

    def contracts(self):
        return None, {"schema": 1, "contracts": []}

    def fx_rates(self):
        return None, {"schema": 1, "usd_jpy": {}}

    def save_versioned(self, kind, doc):
        self.saved.append((kind, doc))
        return f"billing/{kind}/1.json"

    def previous_version(self, kind, key):
        return None

    def compactor_state(self):
        return {}


def client_for(email, store):
    c = TestClient(create_app(store), base_url="http://testserver")
    csrf = "tok"
    data = b64encode(json.dumps({"user": {"email": email, "role": role_of(email)}, "csrf": csrf}).encode())
    c.cookies.set("lab_audit_session", TimestampSigner("unit-secret").sign(data).decode(), path="/lab-audit")
    return c, {"x-csrf-token": csrf}


class ViewerTest(unittest.TestCase):
    def test_signin_required(self):
        c = TestClient(create_app(FakeStore()))
        self.assertEqual(c.get("/lab-audit/api/usage").status_code, 401)
        self.assertEqual(c.get("/lab-audit/", follow_redirects=False).status_code, 307)

    def test_user_sees_only_own_rows(self):
        c, _ = client_for("a@x", FakeStore())
        rows = c.get("/lab-audit/api/usage?user_email=b@x").json()["rows"]
        self.assertEqual([r["user_email"] for r in rows], ["a@x"])     # 他人を指定しても自分に固定
        self.assertEqual([r["user_email"] for r in c.get("/lab-audit/api/settlement").json()["rows"]], ["a@x"])
        c2, h2 = client_for("b@x", FakeStore())
        self.assertEqual(c2.get("/lab-audit/api/requests/c1?at=2026-10-11T01:00:00%2B00:00").status_code, 404)
        self.assertEqual(c2.get("/lab-audit/api/views").status_code, 403)
        self.assertEqual(c2.put("/lab-audit/api/contracts", json={"contracts": []}, headers=h2).status_code, 403)

    def test_body_requires_view_record_and_csrf(self):
        store = FakeStore()
        c, h = client_for("a@x", store)
        url = "/lab-audit/api/requests/c1/body?at=2026-10-11T01:00:00%2B00:00"
        self.assertEqual(c.post(url, json={}).status_code, 403)               # CSRF の値が無い
        r = c.post(url, json={}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(store.views[0][1]["viewer_email"], "a@x")
        store.fail_write = True
        self.assertEqual(c.post(url, json={}, headers=h).status_code, 503)   # 記録を書けなければ本文を返さない

    def test_auditor_needs_reason_for_others(self):
        store = FakeStore()
        c, h = client_for("aud@x", store)
        url = "/lab-audit/api/requests/c1/body?at=2026-10-11T01:00:00%2B00:00"
        self.assertEqual(c.post(url, json={}, headers=h).status_code, 400)
        self.assertEqual(c.post(url, json={"reason": "監査"}, headers=h).status_code, 200)
        self.assertEqual(store.views[0][1]["reason"], "監査")

    def test_admin_contracts_validation_and_log(self):
        store = FakeStore()
        c, h = client_for("admin@x", store)
        bad = {"contracts": [{"id": "a", "kind": "x", "currency": "USD", "models": []}]}
        self.assertEqual(c.put("/lab-audit/api/contracts", json=bad, headers=h).status_code, 400)
        good = {"contracts": [{"id": "a", "kind": "invoice", "currency": "JPY", "models": ["m"], "fees": {}}]}
        self.assertEqual(c.put("/lab-audit/api/contracts", json=good, headers=h).status_code, 200)
        self.assertEqual(store.views[-1][0], "admin")
        self.assertEqual(c.put("/lab-audit/api/fx-rates/2026-10", json={"usd_jpy": 150}, headers=h).status_code, 200)
        self.assertEqual(c.put("/lab-audit/api/fx-rates/2026-13", json={"usd_jpy": 150}, headers=h).status_code, 400)

    def test_range_limit(self):
        c, _ = client_for("a@x", FakeStore())
        self.assertEqual(c.get("/lab-audit/api/usage?start=2026-01-01&end=2026-10-01").status_code, 400)


if __name__ == "__main__":
    unittest.main()
