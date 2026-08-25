"""共通ソース管理者（source_manager）の権限テスト。

何を守りたいか:
    1. 共通ソースのアップロードは admin と source_manager だけができること
       （employee や未知のロール名では絶対に通らない）
    2. 個別ソース（scope='individual'）の登録は admin だけができること。
       個別ソースは特定の社員に紐づく資料（評価・給与など）なので、
       source_manager にも触らせない
    3. source_manager が「共通ソースを増やす」以外の管理者機能
       （スタッフ一覧・ダウンロード）に手を出せないこと
    4. 削除は source_manager でもできるが、共通ソースに限られること（Issue #118）。
       他人の個別ソース（評価・給与など）は admin だけが削除できる

DBを使うテストと使わないテストが混在している理由:
    1〜3で確かめたいのは「拒否されること」だけで、拒否は必ずDBに触る前に起きる。
    - require_source_uploader / require_admin … ハンドラに入る前に403
    - check_scope_permission … ハンドラ冒頭、DB接続より前に403
    そのため temp_db を使わず、テスト用DBが無い環境でも実行できる。

    一方4（削除）の判定は、DBから取った行の scope を見て決まる
    （source_id を受け取った時点では共通か個別か分からない）。
    許可される側と拒否される側の両方を通すには実際に行が必要なので、
    このセクションだけ temp_db を使う。

    アップロードが成功する経路（DB・ストレージ・ベクトル化が絡む）は
    tests/test_sources_upload.py が受け持つ。
"""

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import database, storage
from app.main import app
from app.routers import sources_router
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
    ],
)
def test_共通ソース管理者は管理者専用APIを叩けない(method: str, path: str) -> None:
    """source_manager に許すのは共通ソースの登録と削除だけで、他の管理者機能は admin のまま。

    特にダウンロードは、中身がそのまま手に入るため他人の個別ソース
    （評価・給与など）に届いてしまう。require_admin を外していない。
    存在しない source_id を指定しても、404ではなく403が返るのが正しい
    （権限判定はDBを引く前に終わっているので、ソースの有無を教えない）。

    削除（DELETE /api/sources/{source_id}）をこの一覧から外した理由:
        Issue #118 で source_manager にも共通ソースの削除を許したため、
        入口の依存が require_admin から require_source_uploader に変わった。
        「叩けない」ではなく「共通ソースだけ削除できる」が正しい仕様になったので、
        判定を確かめるテストは下の削除セクションへ移した。
    """
    response = TestClient(app).request(method, path, headers=_headers("8", ROLE_SOURCE_MANAGER))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "管理者のみ操作できます"


# --- 4. 削除の権限テスト（Issue #118） -----------------------------------------


@pytest.fixture
def 外部への削除呼び出し(
    monkeypatch: pytest.MonkeyPatch, temp_db: None
) -> dict[str, list[str]]:
    """Qdrantとストレージへの実通信を止め、「呼ばれたかどうか」だけを記録する。

    出力:
        {"qdrant": [呼ばれた source_id...], "storage": [呼ばれた file_path...]} の辞書

    なぜ差し替えるか:
        delete_source は Qdrant → 実体 → DB の順に消しに行く。
        本物のままだと、テストのたびに Qdrant とGCSへ実通信してしまう。
        ここで確かめたいのは権限判定であって、削除処理の中身ではない。

    なぜ「呼ばれたか」を記録するか:
        403で弾かれたケースでは、DBの行が残っているだけでは不十分で、
        Qdrantのベクトルや実体まで到達していないことも確かめたい。
        判定より後ろの処理が1つでも動いていたら、判定の位置が間違っている。

    差し替え先について:
        delete_by_source_id は sources_router が名前で取り込んでいるので、
        取り込んだ側（sources_router）の属性を差し替える。
        storage.delete は storage モジュール経由で呼ばれているので、
        そちらのモジュール属性を差し替える。
    """
    calls: dict[str, list[str]] = {"qdrant": [], "storage": []}

    def fake_delete_by_source_id(source_id: str) -> None:
        calls["qdrant"].append(source_id)

    def fake_storage_delete(file_path: str) -> None:
        calls["storage"].append(file_path)

    monkeypatch.setattr(sources_router, "delete_by_source_id", fake_delete_by_source_id)
    monkeypatch.setattr(storage, "delete", fake_storage_delete)

    # 後始末（yield して teardown を書く形）が要らない理由:
    #     1. monkeypatch の差し替えは、pytest がテスト終了時に自動で元へ戻す
    #     2. このファイルは _headers() で本物のJWTを作って権限判定を通しており、
    #        app.dependency_overrides を一度も設定していない
    #        （他のテストファイルにある dependency_overrides.clear() は、
    #         差し替えを行っているあちら側で必要な後始末で、ここでは消す対象が無い）
    #     戻すものが無いので、そのまま return してよい
    return calls


def _insert_source(scope: str, owner_user_id: str | None = None) -> int:
    """sources テーブルに1行だけ直接登録して source_id を返す。

    入力:
        scope         … 'common'（全社共通）か 'individual'（社員個別）
        owner_user_id … 個別ソースの持ち主。共通ソースなら None

    出力:
        採番された source_id

    アップロードAPI経由にしない理由:
        source_manager は個別ソースを登録できない（check_scope_permission が弾く）。
        つまりAPI経由では「source_manager から見た他人の個別ソース」を用意できない。
        削除の判定を確かめるにはその行が要るので、DBへ直接入れる。

    file_path について:
        ストレージへの削除は差し替え済みなので実体は要らないが、
        本番と同じ形（file_type='txt' の行）にして削除処理を素通りさせる。
    """
    conn = database.get_connection()
    cursor = conn.execute(
        """
        INSERT INTO sources
            (file_name, file_type, file_path, scope, owner_user_id, uploaded_at, uploaded_by)
        VALUES (%s, 'txt', %s, %s, %s, '2026-01-01T00:00:00+00:00', 'ADMIN')
        RETURNING source_id
        """,
        (f"テスト資料_{scope}.txt", f"uploads/テスト資料_{scope}.txt", scope, owner_user_id),
    )
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    assert row is not None
    source_id: int = row["source_id"]
    return source_id


def _source_exists(source_id: int) -> bool:
    """指定した source_id の行がDBに残っているかを返す。

    削除を防げているかは、ステータスコードだけでは分からない。
    403を返しつつ行は消えている、という壊れ方を捕まえるために直接DBを見る。
    """
    conn = database.get_connection()
    row = conn.execute(
        "SELECT source_id FROM sources WHERE source_id = %s", (source_id,)
    ).fetchone()
    conn.close()
    return row is not None


def test_共通ソース管理者は共通ソースを削除できる(
    外部への削除呼び出し: dict[str, list[str]],
) -> None:
    """Issue #118 の本題。scope='common' なら source_manager でも削除できる。"""
    source_id = _insert_source("common")

    response = TestClient(app).delete(
        f"/api/sources/{source_id}", headers=_headers("8", ROLE_SOURCE_MANAGER)
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "source_id": source_id}

    # DBの行が消えている
    assert _source_exists(source_id) is False

    # Qdrantのベクトルと実体にも削除が届いている
    # （DBの行だけ消えると、消したはずの資料をAIが参照し続ける）
    assert 外部への削除呼び出し["qdrant"] == [str(source_id)]
    assert len(外部への削除呼び出し["storage"]) == 1


def test_共通ソース管理者は個別ソースを削除できない(
    外部への削除呼び出し: dict[str, list[str]],
) -> None:
    """scope='individual' を指定すると403。行も消えていない。

    ステータスコードだけを見ないのが重要:
        権限判定の目的は「403を返すこと」ではなく「削除させないこと」。
        判定を入れる位置を間違えて削除処理の後ろに置いてしまうと、
        403は返るのに資料は消えている、という最悪の壊れ方になる。
        DBの行と、Qdrant・ストレージへの呼び出しの両方で確かめる。
    """
    source_id = _insert_source("individual", owner_user_id="2")

    response = TestClient(app).delete(
        f"/api/sources/{source_id}", headers=_headers("8", ROLE_SOURCE_MANAGER)
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "このソースを削除する権限がありません"

    # DBの行が残っている（削除されていない）
    assert _source_exists(source_id) is True

    # 判定より後ろの削除処理が1つも動いていない
    assert 外部への削除呼び出し["qdrant"] == []
    assert 外部への削除呼び出し["storage"] == []


def test_管理者は個別ソースを削除できる(
    外部への削除呼び出し: dict[str, list[str]],
) -> None:
    """Issue #118 の変更で、社長のこれまでの権限が狭まっていないことを確かめる。

    依存を require_admin から require_source_uploader に緩めたうえで
    ハンドラ内に判定を足したため、admin 側を素通しし損ねていないかを固定する。
    """
    source_id = _insert_source("individual", owner_user_id="2")

    response = TestClient(app).delete(
        f"/api/sources/{source_id}", headers=_headers("ADMIN", ROLE_ADMIN)
    )

    assert response.status_code == 200, response.text
    assert _source_exists(source_id) is False
    assert 外部への削除呼び出し["qdrant"] == [str(source_id)]


def test_社員はソースを削除できない() -> None:
    """employee は入口（require_source_uploader）で403。

    temp_db を使わないのは、この拒否がハンドラに入る前に起きるため。
    存在しない source_id を指定しても404ではなく403が返るのが正しい
    （権限判定がDBを引く前に終わっている＝ソースの有無を教えない）。
    """
    response = TestClient(app).delete(
        "/api/sources/99999", headers=_headers("1", ROLE_EMPLOYEE)
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "共通ソースをアップロードする権限がありません"
