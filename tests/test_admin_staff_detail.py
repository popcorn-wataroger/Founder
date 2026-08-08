"""社員データ画面が使う管理者APIのテスト。

対象:
    GET /api/admin/users/{user_id}          … 社員1人分の基本情報
    GET /api/admin/users/{user_id}/sources  … その社員の個別ソース一覧

何を守りたいか:
    1. パスワードなど、画面に不要な項目がレスポンスに混ざらないこと
    2. 他人の個別ソースや全社共通ソースが、その社員の欄に出てこないこと
    3. 社員（employee）がこれらのURLを叩いても拒否されること
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.routers.auth_router import create_access_token, require_admin


@pytest.fixture
def client() -> Iterator[TestClient]:
    """管理者としてログイン済みの状態でAPIを叩けるクライアント。

    認証そのものはここでの検証対象ではないので、require_admin を差し替えて通す。
    （権限チェックが効いているかは admin_required のテストで別途確認する）
    """
    app.dependency_overrides[require_admin] = lambda: {"user_id": "1", "role": "admin"}
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


def test_社員の基本情報が返る(client: TestClient, temp_db: None) -> None:
    """EMP001（user_id=2）の基本情報が、想定どおりの項目で返る。

    最終ログインを user_logins テーブルから読むようになったため、
    このテストも空のテーブルから始められるよう temp_db を使う。
    """
    response = client.get("/api/admin/users/2")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["employee_code"] == "EMP001"
    assert body["department"]
    assert set(body.keys()) == {
        "user_id",
        "employee_code",
        "name",
        "department",
        "gender",
        "birth_date",
        "family",
        "hire_date",
        "employment_type",
        "last_login_at",
    }


def test_パスワードとroleは返さない(client: TestClient, temp_db: None) -> None:
    """CSVに password 列があるので、レスポンスに漏れていないことを固定する。"""
    response = client.get("/api/admin/users/2")

    assert response.status_code == 200
    assert "password" not in response.json()
    assert "role" not in response.json()


def test_存在しないuser_idは404(client: TestClient) -> None:
    response = client.get("/api/admin/users/99999")
    assert response.status_code == 404


def test_管理者本人のuser_idは404(client: TestClient) -> None:
    """スタッフ一覧が admin を除外しているので、詳細も見られない扱いに揃える。"""
    response = client.get("/api/admin/users/1")
    assert response.status_code == 404


def test_個別ソースは本人のものだけ返る(client: TestClient, temp_db: None) -> None:
    """共通ソースと他人の個別ソースが混ざらず、登録日の新しい順に並ぶ。"""
    _insert_source("本人_古い.txt", "individual", "2", "2025-01-01T00:00:00+00:00")
    _insert_source("本人_新しい.txt", "individual", "2", "2025-05-01T00:00:00+00:00")
    _insert_source("他人の評価.txt", "individual", "3", "2025-05-02T00:00:00+00:00")
    _insert_source("就業規則.txt", "common", None, "2025-05-03T00:00:00+00:00")

    response = client.get("/api/admin/users/2/sources")

    assert response.status_code == 200, response.text
    sources = response.json()

    # 本人の個別ソースだけ、新しい順で返る
    assert [s["file_name"] for s in sources] == ["本人_新しい.txt", "本人_古い.txt"]
    assert all(s["scope"] == "individual" for s in sources)
    assert all(s["owner_user_id"] == "2" for s in sources)

    # file_path（サーバー内部の保存先）は返さない。
    # 画面には不要な情報で、外に出すと保存場所の構造が分かってしまうため
    assert all("file_path" not in s for s in sources)


def test_個別ソースが0件でも空リストを返す(client: TestClient, temp_db: None) -> None:
    """他人のソースだけがある状態で、本人の欄には何も出てこない。"""
    _insert_source("他人の評価.txt", "individual", "3", "2025-05-02T00:00:00+00:00")

    response = client.get("/api/admin/users/2/sources")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("path", ["/api/admin/users/2", "/api/admin/users/2/sources"])
def test_社員は403で拒否される(path: str) -> None:
    """社員のトークンでは、どちらのURLも叩けない（require_admin が効いている）。"""
    token = create_access_token(user_id="2", role="employee")
    response = TestClient(app).get(path, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
