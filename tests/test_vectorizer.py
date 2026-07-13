"""_is_safe_public_url（SSRF対策の関門）のテスト。

なぜモックするか:
    この関数は socket.getaddrinfo で「ホスト名 → IPアドレス」を解決する。
    テストで本物のDNSを引くと、ネットワークが無いCI環境では落ちるし、
    「example.com が何のIPに解決されるか」も将来変わりうる。
    そこで getaddrinfo を差し替え（モックし）、
    「このホスト名はこのIPに解決されたことにする」と決め打ちして、
    IPの判定ロジックだけを検証する。
"""

import socket

import pytest

from app import vectorizer


def _fake_addr_infos(*ips: str) -> list[tuple]:
    """getaddrinfo が返す形（5要素タプルのリスト）を組み立てるテスト用ヘルパー。

    入力:
        ips … 解決されたことにしたいIPアドレス文字列（複数可）

    出力:
        getaddrinfo と同じ形のリスト。
        本物は (family, type, proto, canonname, sockaddr) の5要素で、
        検査対象は最後の sockaddr の先頭＝IPアドレスだけ。
    """
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in ips]


def _patch_getaddrinfo(monkeypatch: pytest.MonkeyPatch, *ips: str) -> None:
    """getaddrinfo を「指定したIPに解決する」偽物に差し替える。"""

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list[tuple]:
        return _fake_addr_infos(*ips)

    monkeypatch.setattr(vectorizer.socket, "getaddrinfo", fake_getaddrinfo)


# --- スキームの検査（名前解決までいかずに弾かれる） ---


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # ローカルファイルを読ませる攻撃
        "gopher://example.com/",  # 古いプロトコル経由の攻撃
        "ftp://example.com/",
        "example.com",  # スキームが無い
    ],
)
def test_非http_httpsスキームは拒否される(url: str) -> None:
    assert vectorizer._is_safe_public_url(url) is False


# --- 内部向けIPに解決されるホスト名は拒否される ---


@pytest.mark.parametrize(
    ("ip", "理由"),
    [
        ("127.0.0.1", "loopback（サーバー自身）"),
        ("169.254.169.254", "link-local（クラウドのメタデータサーバー）"),
        ("10.0.0.5", "private（社内LAN）"),
        ("192.168.1.1", "private（社内LAN）"),
        ("172.16.0.1", "private（社内LAN）"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
    ],
)
def test_内部向けIPに解決されるURLは拒否される(
    monkeypatch: pytest.MonkeyPatch, ip: str, 理由: str
) -> None:
    _patch_getaddrinfo(monkeypatch, ip)
    assert vectorizer._is_safe_public_url("http://evil.example/") is False, 理由


def test_公開IPと内部IPが混ざる場合も拒否される(monkeypatch: pytest.MonkeyPatch) -> None:
    """1つのホスト名が複数IPを持つとき、1つでも内部IPがあれば通さない。

    攻撃者は「公開IPと内部IPの両方を返すDNS」を用意できるため、
    全IPを検査して1つでも危険なら拒否する必要がある。
    """
    _patch_getaddrinfo(monkeypatch, "93.184.216.34", "127.0.0.1")
    assert vectorizer._is_safe_public_url("http://evil.example/") is False


# --- 名前解決に失敗した場合 ---


def test_名前解決に失敗したURLは拒否される(monkeypatch: pytest.MonkeyPatch) -> None:
    """安全か判断できないURLは、安全側に倒して拒否する。"""

    def raise_gaierror(*args: object, **kwargs: object) -> list[tuple]:
        raise socket.gaierror("名前解決に失敗しました")

    monkeypatch.setattr(vectorizer.socket, "getaddrinfo", raise_gaierror)
    assert vectorizer._is_safe_public_url("http://not-exist.example/") is False


# --- 公開IPに解決されるURLだけが通る ---


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_公開IPに解決されるURLは許可される(monkeypatch: pytest.MonkeyPatch, scheme: str) -> None:
    _patch_getaddrinfo(monkeypatch, "93.184.216.34")
    assert vectorizer._is_safe_public_url(f"{scheme}://example.com/page") is True
