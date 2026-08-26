"""URL登録API（POST /api/sources/url）のSSRF対策のテスト。

何を守りたいか:
    登録するURLは社長がフォームに自由に入力する値。何の検査もせずに取りにいくと、
    サーバー自身に「外からは見えない内部アドレス」へアクセスさせられてしまう
    （SSRF＝サーバー側リクエスト偽造）。
    たとえば http://169.254.169.254/ はクラウド(GCP等)のメタデータサーバーで、
    サービスアカウントのアクセストークンが取れてしまう。
    http://127.0.0.1:8000/ や社内ネットワークのIPも同様に危険。
    こうしたURLが、登録の入口で400として弾かれることを固定する。

なぜDBを使わないか:
    URLの検証はDB登録より前に置いてある。拒否される経路はDBに一度も触らないので、
    temp_db を付けずに実行できる（テスト用PostgreSQLが無い環境でも動く）。
    「本当にDBより前で弾いているか」自体も、get_connection を呼ばれたら失敗する
    関数に差し替えることで確かめている。

検証の実体との役割分担:
    「そのURLが安全か」を判断するのは app/safe_urls.py の build_safe_public_url。
    スキーム・内部IP・ポート・認証情報の細かい判定は
    tests/test_vectorizer.py が受け持つ。
    このファイルが確かめるのは、APIの入口でその検証が実際に呼ばれていて、
    弾かれたときに400になる、というつなぎ込みの部分だけ。
"""

import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app import safe_urls
from app.main import app
from app.routers import sources_router
from app.routers.auth_router import ROLE_CEO, create_access_token

# 管理者としてログイン済みとみなすときの user_id（users.csv の ADMIN）
ADMIN_USER_ID = "1"


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    入力:
        user_id … トークンに載せる user_id
        role    … トークンに載せるロール名

    出力:
        {"Authorization": "Bearer <JWT>"} の形の辞書

    なぜ dependency_overrides ではなく本物のトークンを使うか:
        差し替えは検証したい処理を飛ばしてしまうことがある。
        ここでは管理者として通り抜けたうえで「URL検証で弾かれる」ことを見たいので、
        認証は本物のまま通す。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _register_url(url: str) -> httpx.Response:
    """管理者としてURL登録APIを叩く（テスト用の短縮形）。

    ADMIN のトークンを使うのは、権限（403）ではなくURL検証（400）で
    弾かれていることを確かめたいため。
    """
    return TestClient(app).post(
        "/api/sources/url",
        json={"url": url, "scope": "common"},
        headers=_headers(ADMIN_USER_ID, ROLE_CEO),
    )


def _patch_getaddrinfo(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """名前解決を「指定したIPに解決する」偽物に差し替える。

    本物のDNSを引くと、ネットワークが無いCI環境では落ちるし、
    「そのホスト名が何のIPに解決されるか」も将来変わりうる。
    差し替える先が safe_urls.socket なのは、実際に getaddrinfo を呼ぶのが
    app/safe_urls.py の _resolved_ips_are_public だから。
    """

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(safe_urls.socket, "getaddrinfo", fake_getaddrinfo)


# --- スキームの検査 -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",  # 暗号化されない通信
        "http://127.0.0.1:8000/",  # 内部サービスを指す用途で使われやすい
    ],
)
def test_httpのURLは400で拒否される(url: str) -> None:
    """https 以外は受け付けない。

    http を許さない理由:
        通信内容が暗号化されないため、経路上で書き換えられた内容を
        そのままベクトル化して社内ナレッジに取り込んでしまう恐れがある。
    """
    response = _register_url(url)

    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # ローカルファイルを読ませる攻撃
        "gopher://example.com/",  # 古いプロトコル経由の攻撃
        "ftp://example.com/",
        "example.com",  # スキームが無い
    ],
)
def test_https以外のスキームは400で拒否される(url: str) -> None:
    response = _register_url(url)

    assert response.status_code == 400, response.text


# --- 内部向けIPの検査 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("ip", "理由"),
    [
        ("127.0.0.1", "loopback（サーバー自身）"),
        ("10.0.0.1", "private（社内LAN）"),
        ("192.168.1.1", "private（社内LAN）"),
        ("169.254.169.254", "link-local（クラウドのメタデータサーバー）"),
    ],
)
def test_内部向けIPに解決されるURLは400で拒否される(
    monkeypatch: pytest.MonkeyPatch, ip: str, 理由: str
) -> None:
    """見た目が公開URLでも、解決先が内部IPなら弾く。

    スキームは https にしてある。http だとスキーム検査で先に弾かれてしまい、
    ここで確かめたいIPの検査を通らないため。
    """
    _patch_getaddrinfo(monkeypatch, ip)

    response = _register_url("https://evil.example/")

    assert response.status_code == 400, response.text


# --- 認証情報付きURLの検査 ------------------------------------------------------


def test_認証情報付きURLは400で拒否される() -> None:
    """user:pass@host 形式は、意図しない認証情報の送信につながるため拒否する。

    名前解決を差し替えていない理由:
        認証情報の検査は、DNS解決より前に置いてある（app/safe_urls.py）。
        文字列だけで判定できる検査を先に済ませることで、必ず拒否されるURLでも
        攻撃者が指定したホスト名へDNS問い合わせを送ってしまうことを防いでいる。
        差し替えなしで通るということ自体が、その順序が保たれている証拠になる。
    """
    response = _register_url("https://user:pass@example.com/")

    assert response.status_code == 400, response.text


# --- 拒否はDBに触る前に起きる ---------------------------------------------------


def test_拒否されたURLはDBに行を作らない(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL検証がDB登録より前にあることを確かめる。

    なぜ重要か:
        検証がDB登録より後だと、不正なURLでも一度は行が作られ、
        失敗してからロールバックされることになる。
        ロールバック自体が失敗すれば、不正なURLの行が残ってしまう。
        入口で弾いていれば、そもそも書き込みが起きない。

    どうやって確かめるか:
        sources_router が使う get_connection を「呼ばれたら失敗する関数」に
        差し替える。400が返るなら、DB接続に到達せず弾かれたということ。
    """

    def fail_get_connection() -> None:
        raise AssertionError("URLの検証より前にDBへ接続してはいけない")

    monkeypatch.setattr(sources_router, "get_connection", fail_get_connection)

    response = _register_url("http://example.com/")

    assert response.status_code == 400, response.text


def test_内部IPに解決されるURLもDBに行を作らない(monkeypatch: pytest.MonkeyPatch) -> None:
    """スキーム以外の検査（IP判定）もDB登録より前にあることを確かめる。

    直前のテストは http:// を使っているため、スキーム検査の時点で弾かれる。
    それだけだと、将来IP検査だけがDB登録の後ろへ移動しても気づけない。
    スキーム検査を通過したうえでIP検査で弾かれるケースも固定しておく。
    """

    def fail_get_connection() -> None:
        raise AssertionError("URLの検証より前にDBへ接続してはいけない")

    monkeypatch.setattr(sources_router, "get_connection", fail_get_connection)
    _patch_getaddrinfo(monkeypatch, "169.254.169.254")

    response = _register_url("https://evil.example/")

    assert response.status_code == 400, response.text
