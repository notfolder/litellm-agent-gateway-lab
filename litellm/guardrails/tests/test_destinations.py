"""宛先の許可リストの単体テスト（モデルを呼ばない）。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import destinations as ds  # noqa: E402


class HostOfTest(unittest.TestCase):
    def test_forms(self):
        cases = {
            "https://Upload.Partner.example:8443/api/x": "upload.partner.example",
            "http://user:pw@10.0.0.5/": "10.0.0.5",
            "crm.corp.example:443": "crm.corp.example",
            "[fe80::1]:8080": "fe80::1",
            "app@10.20.3.15": "10.20.3.15",
            "example.net.": "example.net",
            "unknown": "unknown",
            "": "",
            "postgresql://app@10.20.3.15/crm": "10.20.3.15",
        }
        for src, want in cases.items():
            self.assertEqual(ds.host_of(src), want, src)


class AllowlistTest(unittest.TestCase):
    def test_private(self):
        a = ds.Allowlist(["private"])
        for host in ("10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1", "127.0.0.1", "169.254.1.1",
                     "fc00::1", "fd12::3", "::1", "fe80::1", "localhost", "http://LOCALHOST:8080/"):
            self.assertTrue(a.allows(host), host)
        for host in ("8.8.8.8", "172.32.0.1", "2001:db8::1", "example.com", "unknown", ""):
            self.assertFalse(a.allows(host), host)

    def test_cidr_and_names(self):
        a = ds.Allowlist(["203.0.113.0/24", "198.51.100.7", "corp.example", "Upload.Partner-Files.example.net"])
        self.assertTrue(a.allows("203.0.113.9"))
        self.assertTrue(a.allows("198.51.100.7"))
        self.assertFalse(a.allows("203.0.114.1"))
        self.assertTrue(a.allows("corp.example"))
        self.assertTrue(a.allows("https://CRM.corp.example/x"))
        self.assertFalse(a.allows("notcorp.example"))
        self.assertFalse(a.allows("corp.example.attacker.net"))
        self.assertTrue(a.allows("upload.partner-files.example.net:443"))
        self.assertFalse(a.allows("10.0.0.1"))                 # private を書いていない

    def test_no_name_resolution(self):
        a = ds.Allowlist(["corp.example"])
        self.assertFalse(a.allows("10.0.0.1"))                 # 名前の項目と IP アドレスは照合しない
        self.assertFalse(ds.Allowlist(["10.0.0.0/8"]).allows("corp.example"))

    def test_everything_and_empty(self):
        self.assertTrue(ds.Allowlist(["*"]).allows("anything.example"))
        self.assertFalse(ds.Allowlist(["*"]).allows(""))   # 空は宛先ではない（呼ぶ側が照合しない）
        self.assertFalse(ds.Allowlist([]).allows("10.0.0.1"))

    def test_bad_items(self):
        for item in ("10.0.0.0/33", "a b", "corp_example", "http://x"):
            with self.assertRaises(ValueError, msg=item):
                ds.Allowlist([item])

    def test_describe(self):
        d = ds.Allowlist(["private", "corp.example", "203.0.113.0/24"]).describe()
        self.assertIn("プライベートアドレス", d)
        self.assertIn("corp.example", d)
        self.assertIn("203.0.113.0/24", d)
        self.assertIn("すべての宛先", ds.Allowlist(["*"]).describe())


if __name__ == "__main__":
    unittest.main()
