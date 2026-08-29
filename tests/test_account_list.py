"""GET /api/admin/accounts（アカウント管理用の社員一覧）のテスト（Issue #123 段階4）。

対象:
    GET /api/admin/accounts … システム管理者がアカウントを管理するための一覧

何を守りたいか:
    1. システム管理者だけが取得できること（社員・共通ソース管理者・社長は403）
    2. 社長とシステム管理者も一覧に含まれること。
       この2人が漏れると、その2人のパスワードだけ画面から復旧できなくなる
    3. role が実効ロール（user_roles の上書きを反映した値）であること。
       素の値を返すと、変更したロールが画面に反映されない
    4. 平文パスワードが返らないこと
    5. 社長のスタッフ一覧（GET /api/admin/users）の挙動が変わっていないこと。
       用途の違う2本を1本に兼ねていないことを、両方の応答で確かめる

DBを使う:
    users / user_roles テーブルを読むため、すべて temp_db を付けている。
"""

from collections.abc import Iterator

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
from app.user_roles import set_role

# data/users.csv の (user_id, employee_code, role)
ADMIN = ("9", "SYSADMIN", ROLE_ADMIN)
CEO = ("1", "ADMIN", ROLE_CEO)
EMPLOYEE = ("2", "EMP001", ROLE_EMPLOYEE)
SOURCE_MANAGER = ("8", "EMP007", ROLE_SOURCE_MANAGER)

# data/users.csv に入っている人数
社員マスタの人数 = 9


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    dependency_overrides で差し替えないのは、このファイルが確かめたいものに
    「そのロールで通るか／弾かれるか」が含まれているため。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _admin_headers() -> dict[str, str]:
    """システム管理者としてのヘッダ。"""
    return _headers(ADMIN[0], ADMIN[2])


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)
    app.dependency_overrides.clear()


def _get_accounts(client: TestClient) -> list[dict]:
    """一覧を取得して中身を返す（システム管理者として）。"""
    response = client.get("/api/admin/accounts", headers=_admin_headers())
    assert response.status_code == 200, response.text
    accounts: list[dict] = response.json()
    return accounts


# --- A. 取得できること -------------------------------------------------------------


def test_システム管理者は一覧を取得できる(client: TestClient, temp_db: None) -> None:
    """この画面を開けるのは admin だけなので、admin で通らないと機能しない。"""
    response = client.get("/api/admin/accounts", headers=_admin_headers())

    assert response.status_code == 200, response.text
    assert len(response.json()) == 社員マスタの人数


def test_返る項目は4つだけ(client: TestClient, temp_db: None) -> None:
    """平文パスワードが混ざらないことを、キーの集合そのもので固定する。

    「password が無いこと」だけを確かめると、
    生年月日や家族構成が増えたときに気づけない。
    """
    アカウント = _get_accounts(client)[0]

    assert set(アカウント.keys()) == {"user_id", "employee_code", "name", "role"}


def test_社長とシステム管理者も含まれる(client: TestClient, temp_db: None) -> None:
    """ここが GET /api/admin/users との一番の違い。

    この2人が漏れると、その2人のパスワードだけ画面から上書きできなくなる。
    """
    社員コード一覧 = [件["employee_code"] for 件 in _get_accounts(client)]

    assert CEO[1] in 社員コード一覧
    assert ADMIN[1] in 社員コード一覧


def test_並び順はuser_idの数値順(client: TestClient, temp_db: None) -> None:
    """get_all_users() の順をそのまま返していることを固定する。

    key=int を指定している理由:
        user_id は TEXT だが、並びは数値順であることを確かめたい。
        key を付けずに sorted() すると文字列順（1, 10, 2 …）と比べることになり、
        10人以上いる状態でこのテストが逆に失敗する。
    """
    user_id一覧 = [件["user_id"] for 件 in _get_accounts(client)]

    assert user_id一覧 == sorted(user_id一覧, key=int)


def test_10人目を追加しても数値順のまま(client: TestClient, temp_db: None) -> None:
    """今回の並び順の修正が効いているかを、実際に確かめられる唯一のテスト。

    なぜアカウントを追加するのか（重要）:
        data/users.csv の9人は user_id が1桁しかないため、
        文字列順で並べても数値順で並べても結果が同じになる。
        つまり9人のままでは、ORDER BY を元の user_id に戻しても
        テストは通ってしまい、修正を守れない。
        user_id が "10" の行を作って初めて、
        文字列順（"10" が "2" の前に来る）との違いが出る。
    """
    追加 = client.post(
        "/api/admin/accounts",
        json={
            "employee_code": "EMP008",
            "name": "新入社員",
            "role": ROLE_EMPLOYEE,
            "password": "newpassword",
        },
        headers=_admin_headers(),
    )
    assert 追加.status_code == 200, 追加.text
    assert 追加.json()["user_id"] == "10"

    user_id一覧 = [件["user_id"] for 件 in _get_accounts(client)]

    # 末尾が "10" であること。文字列順なら "10" は "2" の前（先頭から2番目）に来る
    assert user_id一覧 == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]


# --- B. role が実効ロールであること --------------------------------------------------


def test_上書きが無ければ社員マスタのroleが返る(client: TestClient, temp_db: None) -> None:
    """user_roles に何も無い状態では、users テーブルの role がそのまま出る。"""
    アカウント = {件["user_id"]: 件["role"] for 件 in _get_accounts(client)}

    assert アカウント[EMPLOYEE[0]] == ROLE_EMPLOYEE
    assert アカウント[SOURCE_MANAGER[0]] == ROLE_SOURCE_MANAGER
    assert アカウント[CEO[0]] == ROLE_CEO
    assert アカウント[ADMIN[0]] == ROLE_ADMIN


def test_user_rolesの上書きが反映される(client: TestClient, temp_db: None) -> None:
    """ここが本命。素の users.role を返していると、この変更が一覧に出ない。

    出ないと「ロールを変更したのに画面が変わらない」ように見える。
    """
    set_role(EMPLOYEE[0], ROLE_SOURCE_MANAGER, updated_by=ADMIN[0])

    アカウント = {件["user_id"]: 件["role"] for 件 in _get_accounts(client)}

    assert アカウント[EMPLOYEE[0]] == ROLE_SOURCE_MANAGER


def test_社長を降格させた場合も一覧から消えない(client: TestClient, temp_db: None) -> None:
    """実効ロールが変わっても、一覧は誰も除外しない。

    スタッフ一覧（GET /api/admin/users）は実効ロールで ceo / admin を外すが、
    こちらは除外しないという違いを固定する。
    """
    set_role(CEO[0], ROLE_EMPLOYEE, updated_by=ADMIN[0])

    アカウント = {件["user_id"]: 件["role"] for 件 in _get_accounts(client)}

    assert アカウント[CEO[0]] == ROLE_EMPLOYEE
    assert len(アカウント) == 社員マスタの人数


# --- C. 権限 -----------------------------------------------------------------------


@pytest.mark.parametrize(("user_id", "employee_code", "role"), [EMPLOYEE, SOURCE_MANAGER, CEO])
def test_システム管理者以外は一覧を取得できない(
    client: TestClient, temp_db: None, user_id: str, employee_code: str, role: str
) -> None:
    """社長(ceo)も弾かれる。

    社長が全社員のロールを一覧で見る画面は別にあり（スタッフ一覧）、
    こちらはアカウント管理専用の入口として admin に限定する。
    """
    response = client.get("/api/admin/accounts", headers=_headers(user_id, role))

    assert response.status_code == 403, response.text


def test_未ログインでは一覧を取得できない(client: TestClient, temp_db: None) -> None:
    """Authorization ヘッダが無ければ、そもそも権限判定まで進まない。"""
    response = client.get("/api/admin/accounts")

    assert response.status_code in (401, 403), response.text


# --- D. 既存のスタッフ一覧が変わっていないこと ---------------------------------------


def test_スタッフ一覧は社長専用のまま(client: TestClient, temp_db: None) -> None:
    """GET /api/admin/users に admin を通していないことを確かめる。

    新しいAPIを足したついでにこちらの権限を広げてしまうと、
    システム管理者が業務上の社員データ（部署・雇用形態）を見られるようになる。
    """
    社長として = client.get("/api/admin/users", headers=_headers(CEO[0], ROLE_CEO))
    管理者として = client.get("/api/admin/users", headers=_admin_headers())

    assert 社長として.status_code == 200, 社長として.text
    assert 管理者として.status_code == 403, 管理者として.text


def test_スタッフ一覧は従来どおりceoとadminを除外する(client: TestClient, temp_db: None) -> None:
    """除外の仕様を新APIに合わせて変えていないことを確かめる。"""
    response = client.get("/api/admin/users", headers=_headers(CEO[0], ROLE_CEO))

    社員コード一覧 = [件["employee_code"] for 件 in response.json()]

    assert CEO[1] not in 社員コード一覧
    assert ADMIN[1] not in 社員コード一覧
    assert EMPLOYEE[1] in 社員コード一覧


def test_スタッフ一覧の返り値にroleは含まれない(client: TestClient, temp_db: None) -> None:
    """新APIのために role を足したりしていないことを確かめる。

    2本のAPIが別物であり続けることを、応答の形で固定する。
    """
    response = client.get("/api/admin/users", headers=_headers(CEO[0], ROLE_CEO))

    assert "role" not in response.json()[0]
