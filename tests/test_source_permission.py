"""共通ソース管理者（source_manager）の権限テスト。

何を守りたいか:
    1. 共通ソースのアップロードは admin と source_manager だけができること
       （employee や未知のロール名では絶対に通らない）
    2. 個別ソース（scope='individual'）の登録は admin だけができること。
       個別ソースは特定の社員に紐づく資料（評価・給与など）なので、
       source_manager にも触らせない
    3. source_manager が「共通ソースを増やす」以外の管理者機能
       （スタッフ一覧・ダウンロード・削除）に手を出せないこと

なぜDBを使わないか:
    ここで確かめたいのは「拒否されること」だけで、拒否は必ずDBに触る前に起きる。
    - require_source_uploader / require_admin … ハンドラに入る前に403
    - check_scope_permission … ハンドラ冒頭、DB接続より前に403
    そのため temp_db を使わず、テスト用DBが無い環境でも実行できる。
    アップロードが成功する経路（DB・ストレージ・ベクトル化が絡む）は
    tests/test_sources_upload.py が受け持つ。
"""

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers.auth_router import (
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_SOURCE_MANAGER,
    can_upload_common_source,
    create_access_token,
)
from app.routers.sources_router import check_scope_permission


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    入力:
        user_id … トークンに載せる user_id
        role    … トークンに載せるロール名

    出力:
        {"Authorization": "Bearer <JWT>"} の形の辞書

    なぜ dependency_overrides ではなく本物のトークンを使うか:
        このファイルの検証対象は権限判定そのもの。
        差し替えてしまうと、確かめたい処理を飛ばしてしまう。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


# --- 1. can_upload_common_source() の単体テスト --------------------------------


@pytest.mark.parametrize(
    ("role", "期待"),
    [
        (ROLE_ADMIN, True),
        (ROLE_SOURCE_MANAGER, True),
        (ROLE_EMPLOYEE, False),
        ("manager", False),  # 存在しないロール名
        ("Admin", False),  # 大文字違い（文字列比較なので一致しない）
        ("", False),  # 空文字
    ],
)
def test_共通ソースをアップロードできる役割の判定(role: str, 期待: bool) -> None:
    """許可するのは admin と source_manager だけ。

    未知のロール名や空文字を False 側に倒しておくのが重要で、
    「知らない値は通す」実装にすると、ロールが増えたときに黙って権限が漏れる。
    """
    assert can_upload_common_source(role) == 期待


# --- 2. check_scope_permission() の単体テスト ----------------------------------


@pytest.mark.parametrize("role", [ROLE_SOURCE_MANAGER, ROLE_EMPLOYEE])
def test_個別ソースは管理者以外だと403(role: str) -> None:
    """admin 以外が scope='individual' を指定すると HTTPException(403) になる。"""
    with pytest.raises(HTTPException) as 例外:
        check_scope_permission(role, "individual")

    assert 例外.value.status_code == 403
    assert 例外.value.detail == "個別ソースを登録できるのは管理者のみです"


def test_管理者なら個別ソースを指定できる() -> None:
    """admin は個別ソースを登録できる（例外を投げずに戻ってくる）。"""
    assert check_scope_permission(ROLE_ADMIN, "individual") is None


@pytest.mark.parametrize("role", [ROLE_ADMIN, ROLE_SOURCE_MANAGER])
def test_共通ソースなら通す(role: str) -> None:
    """scope='common' のときは何もしない。

    入口の require_source_uploader で admin / source_manager に絞り込み済みで、
    そのどちらも共通ソースを登録してよいため、ここで重ねて弾く必要がない。
    """
    assert check_scope_permission(role, "common") is None


# --- 3. エンドポイントの権限テスト ---------------------------------------------


def _upload_request(
    client: TestClient, role: str, scope: str, owner_user_id: str | None = None
) -> httpx.Response:
    """指定したロールで POST /api/sources/upload を叩く。

    ファイルを必ず添えるのは、権限で弾かれたのかファイル未添付で422になったのかを
    取り違えないようにするため。
    """
    data = {"scope": scope}
    if owner_user_id is not None:
        data["owner_user_id"] = owner_user_id

    return client.post(
        "/api/sources/upload",
        files={"file": ("就業規則.txt", "これはテスト用の本文です".encode(), "text/plain")},
        data=data,
        headers=_headers("8", role),
    )


def test_共通ソース管理者は個別ソースをアップロードできない() -> None:
    """source_manager が scope='individual' を送ると403。

    owner_user_id を付けているのは、付けないと validate_scope の
    「individual なら owner_user_id が必須」で400になり、
    権限判定まで到達したかどうかが分からなくなるため。
    """
    response = _upload_request(
        TestClient(app), ROLE_SOURCE_MANAGER, "individual", owner_user_id="2"
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "個別ソースを登録できるのは管理者のみです"


def test_社員はソースをアップロードできない() -> None:
    """employee は共通ソースであっても入口（require_source_uploader）で403。"""
    response = _upload_request(TestClient(app), ROLE_EMPLOYEE, "common")

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "共通ソースをアップロードする権限がありません"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/admin/users"),  # スタッフ一覧
        ("GET", "/api/sources/1/download"),  # ソースのダウンロード
        ("DELETE", "/api/sources/1"),  # ソースの削除
    ],
)
def test_共通ソース管理者は管理者専用APIを叩けない(method: str, path: str) -> None:
    """source_manager に許すのは共通ソースの登録だけで、他の管理者機能は admin のまま。

    特にダウンロードと削除は、他人の個別ソース（評価・給与など）に届いてしまうため
    require_admin を外していない。
    存在しない source_id を指定しても、404ではなく403が返るのが正しい
    （権限判定はDBを引く前に終わっているので、ソースの有無を教えない）。
    """
    response = TestClient(app).request(method, path, headers=_headers("8", ROLE_SOURCE_MANAGER))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "管理者のみ操作できます"
