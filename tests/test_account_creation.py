"""POST /api/admin/accounts（アカウント追加）のテスト（Issue #123 段階2）。

対象:
    POST /api/admin/accounts … システム管理者が新しいアカウントを1つ作る

何を守りたいか:
    1. システム管理者だけが作れること（社員・共通ソース管理者・社長は403）
    2. 作ったアカウントで実際にログインできること
       （行を作るだけでなく、初期パスワードが user_passwords に入っている）
    3. 社員コードが重複したら409で止まること（同じコードの社員を2人作らない）
    4. 妥当でない値（知らないロール、短すぎる／長すぎるパスワード、空の社員コード）を
       保存させないこと
    5. user_id が既存の続き（9の次＝10）から振られること

DBを使う:
    users / user_passwords テーブルに書き込むため、すべて temp_db を付けている。
    temp_db は毎テスト users を data/users.csv の9人に戻すので、
    採番も必ず10から始まる。
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.auth_router import (
    ROLE_ADMIN,
    ROLE_CEO,
    ROLE_EMPLOYEE,
    ROLE_SOURCE_MANAGER,
    create_access_token,
)
from app.user_passwords import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH
from app.users import get_user_by_employee_code

# data/users.csv の SYSADMIN（user_id=9）がシステム管理者
ADMIN_USER_ID = "9"

# 追加するアカウントの既定値。テストごとに一部だけ差し替えて使う
NEW_ACCOUNT = {
    "employee_code": "EMP008",
    "name": "新入社員",
    "role": ROLE_EMPLOYEE,
    "password": "newpassword",
}


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    dependency_overrides で差し替えないのは、このファイルが確かめたいものに
    「そのロールで通るか／弾かれるか」が含まれているため。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _admin_headers() -> dict[str, str]:
    """システム管理者としてのヘッダ。"""
    return _headers(ADMIN_USER_ID, ROLE_ADMIN)


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)
    app.dependency_overrides.clear()


def _create(client: TestClient, **上書き: str) -> httpx.Response:
    """既定値をもとにアカウント追加APIを叩く（システム管理者として）。"""
    payload = {**NEW_ACCOUNT, **上書き}
    return client.post("/api/admin/accounts", json=payload, headers=_admin_headers())


# --- A. 正常系 -------------------------------------------------------------------


def test_システム管理者はアカウントを追加できる(client: TestClient, temp_db: None) -> None:
    """作成に成功し、users テーブルに実際の行ができていることまで確かめる。"""
    response = _create(client)

    assert response.status_code == 200, response.text
    結果 = response.json()
    assert 結果["success"] is True
    assert 結果["employee_code"] == "EMP008"
    assert 結果["role"] == ROLE_EMPLOYEE

    # APIの応答だけでなく、DBに入っていることを直接確かめる
    追加された社員 = get_user_by_employee_code("EMP008")
    assert 追加された社員 is not None
    assert 追加された社員["name"] == "新入社員"
    assert 追加された社員["role"] == ROLE_EMPLOYEE


def test_user_idは既存の続きから振られる(client: TestClient, temp_db: None) -> None:
    """data/users.csv は user_id=9 まで使っているので、次は10から始まる。

    採番がシーケンスであることの確認も兼ねる。
    2件続けて作れば10と11になり、同じ番号が2人に渡らない。
    """
    一人目 = _create(client, employee_code="EMP008")
    二人目 = _create(client, employee_code="EMP009")

    assert 一人目.json()["user_id"] == "10"
    assert 二人目.json()["user_id"] == "11"


def test_指定していない基本情報は空文字で入る(client: TestClient, temp_db: None) -> None:
    """部署や生年月日は入力させていないので、null ではなく空文字になる。

    null が入ると社員データAPI（GET /api/admin/users/{user_id}）の応答が
    "" から null に変わり、画面の表示が崩れる。
    """
    _create(client)

    追加された社員 = get_user_by_employee_code("EMP008")
    assert 追加された社員 is not None
    for 列 in ["department", "gender", "birth_date", "family", "hire_date", "employment_type"]:
        assert 追加された社員[列] == "", f"{列} が空文字ではありません"


def test_password列は空文字のまま(client: TestClient, temp_db: None) -> None:
    """平文のパスワードを users に残さない。

    パスワードは user_passwords にハッシュで持つ。
    users.password は data/users.csv から引き継いだ移行用の列で、
    新しいアカウントには値を入れない。
    """
    _create(client)

    追加された社員 = get_user_by_employee_code("EMP008")
    assert 追加された社員 is not None
    assert 追加された社員["password"] == ""


def test_追加したアカウントでログインできる(client: TestClient, temp_db: None) -> None:
    """ここが本命。行ができるだけでは足りず、初期パスワードで入れる必要がある。"""
    _create(client)

    response = client.post(
        "/api/login",
        json={"employee_code": "EMP008", "password": "newpassword"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert response.json()["role"] == ROLE_EMPLOYEE


def test_違うパスワードではログインできない(client: TestClient, temp_db: None) -> None:
    """初期パスワードがそのまま保存されていることの裏返しの確認。

    ログインAPIは失敗しても200を返し、success の値で成否を伝える仕様
    （既存の挙動。ここでは変えていない）。
    """
    _create(client)

    response = client.post(
        "/api/login",
        json={"employee_code": "EMP008", "password": "wrongpassword"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is False


# --- B. 社員コードの重複 -----------------------------------------------------------


def test_社員コードが重複すると409(client: TestClient, temp_db: None) -> None:
    """同じ社員コードの社員が2人できると、ログインでどちらか決まらなくなる。"""
    一回目 = _create(client, employee_code="EMP008")
    assert 一回目.status_code == 200, 一回目.text

    二回目 = _create(client, employee_code="EMP008", name="別の人")

    assert 二回目.status_code == 409, 二回目.text
    assert "EMP008" in 二回目.json()["detail"]


def test_既存の社員コードと重複しても409(client: TestClient, temp_db: None) -> None:
    """data/users.csv に元から居る社員のコードも使えない。"""
    response = _create(client, employee_code="EMP001")

    assert response.status_code == 409, response.text


def test_重複したときに既存の社員が書き換わらない(client: TestClient, temp_db: None) -> None:
    """409で止まるだけでなく、元の行に手を付けていないことを確かめる。"""
    _create(client, employee_code="EMP001", name="乗っ取り", role=ROLE_CEO)

    既存の社員 = get_user_by_employee_code("EMP001")
    assert 既存の社員 is not None
    assert 既存の社員["name"] == "奥村仁哉"
    assert 既存の社員["role"] == ROLE_EMPLOYEE


# --- C. 権限 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_id", "role"),
    [("2", ROLE_EMPLOYEE), ("8", ROLE_SOURCE_MANAGER), ("1", ROLE_CEO)],
)
def test_システム管理者以外はアカウントを追加できない(
    client: TestClient, temp_db: None, user_id: str, role: str
) -> None:
    """社長(ceo)も弾かれる。アカウント管理は admin だけの仕事（CLAUDE.md の権限表）。"""
    response = client.post(
        "/api/admin/accounts",
        json=NEW_ACCOUNT,
        headers=_headers(user_id, role),
    )

    assert response.status_code == 403, response.text
    # 弾かれたのだから、行もできていない
    assert get_user_by_employee_code("EMP008") is None


def test_未ログインではアカウントを追加できない(client: TestClient, temp_db: None) -> None:
    """Authorization ヘッダが無ければ、そもそも権限判定まで進まない。"""
    response = client.post("/api/admin/accounts", json=NEW_ACCOUNT)

    assert response.status_code in (401, 403), response.text
    assert get_user_by_employee_code("EMP008") is None


# --- D. 入力の検証 ---------------------------------------------------------------


@pytest.mark.parametrize("役割", ["", "manager", "ADMIN", "employee ", "社員"])
def test_知らないロール名は400(client: TestClient, temp_db: None, 役割: str) -> None:
    """権限判定が知らない値を持った社員を作らせない。

    ロール名の大文字小文字の違いや前後の空白も通さない
    （VALID_ROLES との完全一致だけを許す）。
    """
    response = _create(client, role=役割)

    assert response.status_code == 400, response.text
    assert get_user_by_employee_code("EMP008") is None


def test_短すぎるパスワードは400(client: TestClient, temp_db: None) -> None:
    """MIN_PASSWORD_LENGTH 未満は拒否する。"""
    短いパスワード = "a" * (MIN_PASSWORD_LENGTH - 1)

    response = _create(client, password=短いパスワード)

    assert response.status_code == 400, response.text
    assert get_user_by_employee_code("EMP008") is None


def test_ちょうど最短の長さのパスワードは通る(client: TestClient, temp_db: None) -> None:
    """境界そのものは弾かない（8文字未満だけを弾く）。"""
    response = _create(client, password="a" * MIN_PASSWORD_LENGTH)

    assert response.status_code == 200, response.text


def test_長すぎるパスワードは400(client: TestClient, temp_db: None) -> None:
    """bcrypt が扱えるのは72バイトまで。

    ここで弾かないと、hash_password() の中で ValueError になって500になる。
    """
    長いパスワード = "a" * (MAX_PASSWORD_BYTES + 1)

    response = _create(client, password=長いパスワード)

    assert response.status_code == 400, response.text
    assert get_user_by_employee_code("EMP008") is None


def test_日本語のパスワードはバイト数で判定される(client: TestClient, temp_db: None) -> None:
    """日本語1文字はUTF-8で3バイト。25文字で75バイトになり、上限を超える。

    文字数だけで見ていると通ってしまい、bcrypt の側で500になる。
    """
    response = _create(client, password="あ" * 25)

    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    ("employee_code", "name"),
    [("", "新入社員"), ("   ", "新入社員"), ("EMP008", ""), ("EMP008", "   ")],
)
def test_社員コードと氏名が空なら400(
    client: TestClient, temp_db: None, employee_code: str, name: str
) -> None:
    """空の社員コードで作ると、そのアカウントでは誰もログインできない。

    前後の空白だけの入力も空として扱う。
    """
    response = _create(client, employee_code=employee_code, name=name)

    assert response.status_code == 400, response.text


def test_社員コードの前後の空白は取り除かれる(client: TestClient, temp_db: None) -> None:
    """コピー&ペーストで紛れた空白のせいでログインできない、を防ぐ。"""
    response = _create(client, employee_code="  EMP008  ")

    assert response.status_code == 200, response.text
    assert response.json()["employee_code"] == "EMP008"
    assert get_user_by_employee_code("EMP008") is not None
