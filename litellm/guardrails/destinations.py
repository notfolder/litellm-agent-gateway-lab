"""送信の宛先の許可リスト（モデルを呼ばない純粋な関数だけを置く。変更設計書 4.10 節）。

審査官が送信から抜き出した宛先（ホスト名か IP アドレス）を、組織の設定の許可リストと機械的に照合する。
- private: プライベートアドレスの範囲（RFC 1918、ループバック、リンクローカル、IPv6 のユニークローカルとループバック）と localhost
- CIDR か IP アドレス: 宛先が IP アドレスのときだけ照合する
- ホスト名・ドメイン: 同じ名前か、その下位の名前（末尾一致）。宛先がホスト名のときだけ照合する
- *: すべて
名前解決はしない。unknown、空、ホストを取り出せないものは許可しない。
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$")
PRIVATE_DESCRIPTION = "プライベートアドレス（10/8, 172.16/12, 192.168/16, ループバック, リンクローカル, IPv6 の ULA）と localhost"
# private の範囲（変更仕様書 7.4 節）。ipaddress の is_private は文書用の範囲（2001:db8::/32 など）も含むので使わない
PRIVATE_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "fc00::/7", "::1/128", "fe80::/10")]


def host_of(destination: str) -> str:
    """URL・"host:port"・"[v6]:port"・"user@host" からホストだけを取り出し、小文字にする。取り出せなければ空。"""
    s = (destination or "").strip()
    if not s:
        return ""
    if "://" in s:
        host = urlsplit(s).hostname or ""
        return host.lower().rstrip(".")
    s = s.split("/", 1)[0]
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    if s.startswith("["):              # [v6]:port
        end = s.find("]")
        return s[1:end].lower() if end > 0 else ""
    if s.count(":") == 1:              # host:port
        s = s.split(":", 1)[0]
    return s.lower().rstrip(".")


def _ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class Allowlist:
    """許可リスト。項目の解析は作るときに行い、解析できない項目は ValueError にする（起動時に気づくため）。"""

    def __init__(self, items: list[str] | tuple[str, ...]):
        self.private = False
        self.everything = False
        self.networks: list = []
        self.names: list[str] = []
        for raw in items:
            item = (raw or "").strip()
            if not item:
                continue
            if item == "private":
                self.private = True
            elif item == "*":
                self.everything = True
            elif "/" in item or _ip(item) is not None:
                try:
                    self.networks.append(ipaddress.ip_network(item, strict=False))
                except ValueError as e:
                    raise ValueError(f"許可リストの項目を解析できません: {item!r}") from e
            elif _HOSTNAME.match(item):
                self.names.append(item.lower())
            else:
                raise ValueError(f"許可リストの項目を解析できません: {item!r}")

    def allows(self, destination: str) -> bool:
        host = host_of(destination)
        if not host or host == "unknown":
            return False
        if self.everything:
            return True
        ip = _ip(host)
        if ip is not None:
            if self.private and any(ip in n for n in PRIVATE_NETWORKS):
                return True
            return any(ip in n for n in self.networks)
        if self.private and host == "localhost":
            return True
        return any(host == n or host.endswith("." + n) for n in self.names)

    def describe(self) -> str:
        """審査官に渡す文（送ってよい宛先の説明。プライベートアドレスは社内、ほかは送信を許された宛先）。"""
        if self.everything:
            return "すべての宛先（許可リストを使わない）"
        parts: list[str] = []
        if self.private:
            parts.append(PRIVATE_DESCRIPTION)
        allowed = [str(n) for n in self.networks] + [f"{n}（とその下位のドメイン）" for n in self.names]
        if allowed:
            parts.append("送信を許された宛先: " + "、".join(allowed))
        return "、".join(parts) if parts else "（無し。どこへも送れない）"
