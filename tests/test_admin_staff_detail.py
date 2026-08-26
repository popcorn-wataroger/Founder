"""社員データ画面が使う管理者APIのテスト。

対象:
    GET /api/admin/users                    … スタッフ一覧
    GET /api/admin/users/{user_id}          … 社員1人分の基本情報
    GET /api/admin/users/{user_id}/sources  … その社員の個別ソース一覧

何を守りたいか:
    1. パスワードなど、画面に不要な項目がレスポンスに混ざらないこと
       （role は画面でロールを表示・変更するために必要なので返す。
         このAPIは require_ceo の社長専用で、社員には届かない）
    2. 他人の個別ソースや全社共通ソースが、その社員の欄に出てこないこと
    3. 社員（employee）がこれらのURLを叩いても拒否されること
    4. 誰を社長として扱うかを、CSVではなく user_roles の上書きを優先して決めること
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.routers.auth_router import create_access_token, require_ceo
from app.user_roles import set_role


@pytest.fixture
def client() -> Iterator[TestClient]:
    """管理者としてログイン済みの状態でAPIを叩けるクライアント。

    認証そのものはここでの検証対象ではないので、require_ceo を差し替えて通す。
    （権限チェックが効いているかは require_ceo のテストで別途確認する）
    """
    app.dependency_overrides[require_ceo] = lambda: {"user_id": "1", "role": "ceo"}
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
        "role",
    }


def test_パスワードは返さない(client: TestClient, temp_db: None) -> None:
    """CSVに password 列があるので、レスポンスに漏れていないことを固定する。

    なぜ password は返さないのに role は返すのか:
        password は画面のどこにも要らないうえ、漏れれば本人になりすませてしまう。
        role は社員データ画面でその社員のロールを表示し、変更する
        （PUT /api/admin/users/{user_id}/role）ために必要な値で、
        現在のロールが分からないと画面は変更後の値を選ばせようがない。
        このAPIは require_ceo を付けた社長専用なので、
        社員が他人のロールを知る経路にはならない。
    """
    response = client.get("/api/admin/users/2")

    assert response.status_code == 200
    assert "password" not in response.json()


def test_roleは実効ロールが返る(client: TestClient, temp_db: None) -> None:
    """返る role は users.csv の値ではなく、DBの上書きを優先した実効ロール。

    なぜ確かめるか:
        CSVの値をそのまま返す実装にすると、ロールを変更しても画面に反映されず、
        社長から見て「変更したのに変わっていない」状態になる。
        どちらを正とするか（DBが正、CSVが既定値）を、ここで固定しておく。

    EMP001（user_id=2）は users.csv では employee。
    これを source_manager に上書きしてから叩く。
    """
    set_role("2", "source_manager", updated_by="1")

    response = client.get("/api/admin/users/2")

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "source_manager"


def test_存在しないuser_idは404(client: TestClient) -> None:
    response = client.get("/api/admin/users/99999")
    assert response.status_code == 404


def test_管理者本人のuser_idは404(client: TestClient, temp_db: None) -> None:
    """スタッフ一覧が ceo を除外しているので、詳細も見られない扱いに揃える。

    判定が実効ロール（user_roles の上書きを優先）に変わったため、
    上書きの無い状態から始められるよう temp_db を使う。
    """
    response = client.get("/api/admin/users/1")
    assert response.status_code == 404


def test_DBでceoに上げた社員はスタッフ一覧に出ない(client: TestClient, temp_db: None) -> None:
    """スタッフ一覧の除外判定も、CSVの role ではなく実効ロールで行う。

    なぜ確かめるか:
        CSVの role で判定すると、DBで ceo に変更した社員が一覧に残り続ける。
        社長がスタッフとして並び、その人の社員データ画面まで開ける状態になる。

    EMP001（user_id=2）は users.csv では employee。これを ceo に上書きする。
    """
    set_role("2", "ceo", updated_by="1")

    response = client.get("/api/admin/users")

    assert response.status_code == 200, response.text
    assert "EMP001" not in [staff["employee_code"] for staff in response.json()]


def test_DBでemployeeに下げた社長はスタッフ一覧に出る(client: TestClient, temp_db: None) -> None:
    """逆向きの取りこぼしも固定する。降ろした社長は普通のスタッフとして扱う。

    なぜ確かめるか:
        CSVの role で判定すると、DBで employee に変更しても一覧から消えたままになり、
        社員データ画面を開く導線が無いので、ログもソースも辿れなくなる。

    ADMIN（user_id=1）は users.csv では ceo。これを employee に上書きする。
    """
    set_role("1", "employee", updated_by="1")

    response = client.get("/api/admin/users")

    assert response.status_code == 200, response.text
    assert "ADMIN" in [staff["employee_code"] for staff in response.json()]


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
    """社員のトークンでは、どちらのURLも叩けない（require_ceo が効いている）。"""
    token = create_access_token(user_id="2", role="employee")
    response = TestClient(app).get(path, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
