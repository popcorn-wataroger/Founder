"""GET /api/sources/common（共通ソース一覧）のテスト。

対象:
    GET /api/sources/common … 全社共通ソースだけを返す

何を守りたいか:
    1. 個別ソースが1件も返らないこと。ファイル名それ自体が
       「◯◯_評価2025.pdf」のような見せてはいけない情報のため、
       件数が0でも「名前が見えない」ことを含めて確かめる
    2. file_path がレスポンスに含まれないこと（保存先の構造を漏らさない）
    3. 社員（employee）が叩いても拒否されること
    4. 既存の GET /api/sources（社長専用・全件）の挙動が変わっていないこと
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.routers.auth_router import (
    ROLE_CEO,
    ROLE_EMPLOYEE,
    ROLE_SOURCE_MANAGER,
    create_access_token,
)


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    dependency_overrides で差し替えないのは、このファイルが確かめたいものに
    「そのロールで通るか／弾かれるか」が含まれているため。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)
    app.dependency_overrides.clear()


def _insert_source(file_name: str, scope: str, owner_user_id: str | None, uploaded_at: str) -> None:
    """テスト用のソースを1件登録する（ベクトル化は通さず、DBに直接入れる）。"""
    conn = database.get_connection()
    conn.execute(
        """
        INSERT INTO sources
            (file_name, file_type, file_path, scope, owner_user_id, uploaded_at, uploaded_by)
        VALUES (%s, 'txt', %s, %s, %s, %s, '1')
        """,
        (file_name, f"uploads/{file_name}", scope, owner_user_id, uploaded_at),
    )
    conn.commit()
    conn.close()


def _insert_mixed_sources() -> None:
    """共通ソース2件と、他人の個別ソース2件を登録する。"""
    _insert_source("就業規則.txt", "common", None, "2026-01-01T00:00:00")
    _insert_source("経費精算マニュアル.txt", "common", None, "2026-01-03T00:00:00")
    _insert_source("EMP001_評価2025.txt", "individual", "2", "2026-01-02T00:00:00")
    _insert_source("EMP002_給与明細.txt", "individual", "3", "2026-01-04T00:00:00")


@pytest.mark.parametrize("role", [ROLE_CEO, ROLE_SOURCE_MANAGER])
def test_共通ソースだけが登録日の新しい順で返る(
    client: TestClient, temp_db: None, role: str
) -> None:
    """ceo と source_manager のどちらでも、返るのは共通ソースだけ。"""
    _insert_mixed_sources()

    response = client.get("/api/sources/common", headers=_headers("8", role))

    assert response.status_code == 200, response.text
    ソース一覧 = response.json()
    assert [件["file_name"] for 件 in ソース一覧] == [
        "経費精算マニュアル.txt",
        "就業規則.txt",
    ]


def test_個別ソースのファイル名がレスポンスに一切現れない(
    client: TestClient, temp_db: None
) -> None:
    """件数だけでなく、レスポンス全体の文字列にも個別ソースの名前が出ないことを確かめる。

    ファイル名それ自体が見せてはいけない情報なので、
    「共通ソースの件数が合っている」だけでは不十分。
    """
    _insert_mixed_sources()

    response = client.get("/api/sources/common", headers=_headers("8", ROLE_SOURCE_MANAGER))

    assert response.status_code == 200, response.text
    assert "評価2025" not in response.text
    assert "給与明細" not in response.text


def test_レスポンスに含まれる項目が想定どおり(client: TestClient, temp_db: None) -> None:
    """返す列は source_id / file_name / file_type / uploaded_at の4つだけ。

    file_path が混ざっていないことを、キーの集合そのもので確かめる。
    """
    _insert_source("就業規則.txt", "common", None, "2026-01-01T00:00:00")

    response = client.get("/api/sources/common", headers=_headers("8", ROLE_SOURCE_MANAGER))

    assert response.status_code == 200, response.text
    ソース一覧 = response.json()
    assert len(ソース一覧) == 1
    assert set(ソース一覧[0].keys()) == {
        "source_id",
        "file_name",
        "file_type",
        "uploaded_at",
    }


def test_共通ソースが1件も無ければ空のリストが返る(client: TestClient, temp_db: None) -> None:
    """個別ソースだけがある状態でも、空のリストになる（個別ソースで埋めない）。"""
    _insert_source("EMP001_評価2025.txt", "individual", "2", "2026-01-02T00:00:00")

    response = client.get("/api/sources/common", headers=_headers("8", ROLE_SOURCE_MANAGER))

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_社員は共通ソース一覧を取得できない(client: TestClient) -> None:
    """employee は入口（require_source_uploader）で403。

    DBを使わないのは、拒否がDBに触る前に起きるため。
    """
    response = client.get("/api/sources/common", headers=_headers("2", ROLE_EMPLOYEE))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "共通ソースをアップロードする権限がありません"


def test_未ログインでは共通ソース一覧を取得できない(client: TestClient) -> None:
    """Authorization ヘッダが無ければ401。"""
    response = client.get("/api/sources/common")

    assert response.status_code == 401, response.text


def test_既存の全ソース一覧は共通ソース管理者には返らない(client: TestClient) -> None:
    """GET /api/sources は require_ceo のまま（今回のIssueで緩めていない）。

    ここが403のままであることが、個別ソースのファイル名と file_path を
    source_manager に見せない根拠になる。
    """
    response = client.get("/api/sources", headers=_headers("8", ROLE_SOURCE_MANAGER))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "管理者のみ操作できます"


def test_管理者は従来どおり全ソースを取得できる(client: TestClient, temp_db: None) -> None:
    """GET /api/sources の挙動が変わっていないこと（共通・個別の両方が返る）。"""
    _insert_mixed_sources()

    response = client.get("/api/sources", headers=_headers("1", ROLE_CEO))

    assert response.status_code == 200, response.text
    assert len(response.json()) == 4
