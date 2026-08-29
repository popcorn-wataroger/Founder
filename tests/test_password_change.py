"""パスワード変更APIのテスト（Issue #123 段階3）。

対象:
    PUT /api/me/password                        … 本人が自分のパスワードを変える（全ロール）
    PUT /api/admin/accounts/{user_id}/password  … システム管理者が強制的に上書きする

何を守りたいか:
    1. 変更が実際に効くこと。応答が success でも、
       新しいパスワードでログインできなければ意味がない
    2. 古いパスワードが使えなくなること（上書きであって追加ではない）
    3. 本人確認なしに他人のパスワードを変えられないこと
       （current_password の照合と、強制上書き側の403）
    4. 保存できない長さのパスワードを受け付けないこと
    5. システム管理者(admin)も自分のパスワードを変えられること
       （業務系APIからは締め出されている役割だが、アカウント管理は別）

DBを使う:
    user_passwords テーブルに書き込むため、すべて temp_db を付けている。
    temp_db は毎テスト user_passwords を空に戻すので、
    どのテストも「まだ一度もパスワードを変えていない」状態から始まる。
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

# data/users.csv の初期パスワード（全員共通のダミー値）
初期パスワード = "password"

# (user_id, employee_code, role) の対応。data/users.csv のとおり
ADMIN = ("9", "SYSADMIN", ROLE_ADMIN)
CEO = ("1", "ADMIN", ROLE_CEO)
EMPLOYEE = ("2", "EMP001", ROLE_EMPLOYEE)
SOURCE_MANAGER = ("8", "EMP007", ROLE_SOURCE_MANAGER)

全ロール = [EMPLOYEE, SOURCE_MANAGER, CEO, ADMIN]


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


def _change_my_password(
    client: TestClient, user_id: str, role: str, current: str, new: str
) -> httpx.Response:
    """本人によるパスワード変更APIを叩く。"""
    return client.put(
        "/api/me/password",
        json={"current_password": current, "new_password": new},
        headers=_headers(user_id, role),
    )


def _reset_password(
    client: TestClient, 対象user_id: str, new: str, 叩く人: tuple[str, str, str] = ADMIN
) -> httpx.Response:
    """強制上書きAPIを叩く（既定ではシステム管理者として）。"""
    user_id, _, role = 叩く人
    return client.put(
        f"/api/admin/accounts/{対象user_id}/password",
        json={"new_password": new},
        headers=_headers(user_id, role),
    )


def _login(client: TestClient, employee_code: str, password: str) -> httpx.Response:
    """ログインAPIを叩く。"""
    return client.post(
        "/api/login",
        json={"employee_code": employee_code, "password": password},
    )


# --- A. 本人によるパスワード変更（PUT /api/me/password）---------------------------


@pytest.mark.parametrize(("user_id", "employee_code", "role"), 全ロール)
def test_全ロールが自分のパスワードを変更できる(
    client: TestClient, temp_db: None, user_id: str, employee_code: str, role: str
) -> None:
    """システム管理者(admin)も含めて、ログインできる人は全員変更できる。

    admin は業務系API（チャット・ソース）からは締め出されているが、
    自分のアカウントの管理は別。ここを弾くと、
    初期パスワードのまま運用し続けることになる。
    """
    response = _change_my_password(
        client, user_id, role, current=初期パスワード, new="newpassword1"
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


@pytest.mark.parametrize(("user_id", "employee_code", "role"), 全ロール)
def test_変更後は新しいパスワードでログインできる(
    client: TestClient, temp_db: None, user_id: str, employee_code: str, role: str
) -> None:
    """ここが本命。応答が success でも、実際に入れなければ意味がない。"""
    _change_my_password(client, user_id, role, current=初期パスワード, new="newpassword1")

    response = _login(client, employee_code, "newpassword1")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


@pytest.mark.parametrize(("user_id", "employee_code", "role"), 全ロール)
def test_変更後は古いパスワードでログインできない(
    client: TestClient, temp_db: None, user_id: str, employee_code: str, role: str
) -> None:
    """追加ではなく上書きであることを確かめる。

    ログインAPIは失敗しても200を返し、success の値で成否を伝える仕様（既存の挙動）。
    """
    _change_my_password(client, user_id, role, current=初期パスワード, new="newpassword1")

    response = _login(client, employee_code, 初期パスワード)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is False


def test_2回続けて変更しても最後のパスワードが有効(client: TestClient, temp_db: None) -> None:
    """2回目は「1回目で設定したパスワード」を現在のパスワードとして通す。

    1回目の変更でハッシュが保存されるので、
    2回目は平文フォールバックではなくハッシュとの照合になる。
    どちらの経路でも同じように動くことを確かめる。
    """
    user_id, employee_code, role = EMPLOYEE

    一回目 = _change_my_password(client, user_id, role, current=初期パスワード, new="firstpass1")
    assert 一回目.status_code == 200, 一回目.text

    二回目 = _change_my_password(client, user_id, role, current="firstpass1", new="secondpass1")
    assert 二回目.status_code == 200, 二回目.text

    assert _login(client, employee_code, "secondpass1").json()["success"] is True
    assert _login(client, employee_code, "firstpass1").json()["success"] is False


def test_現在のパスワードが違うと401(client: TestClient, temp_db: None) -> None:
    """本人確認が効いていないと、トークンを持っているだけで変え放題になる。"""
    user_id, employee_code, role = EMPLOYEE

    response = _change_my_password(client, user_id, role, current="wrongpassword", new="newpass123")

    assert response.status_code == 401, response.text
    # 弾かれたのだから、変わってもいない
    assert _login(client, employee_code, 初期パスワード).json()["success"] is True


def test_トークンの社員が存在しないと401(client: TestClient, temp_db: None) -> None:
    """トークンは正しいが、その user_id の社員が居ない場合。

    パスワードが違う場合と同じ401・同じ文言にしてある。
    分けて返すと「この user_id は存在しない」を外から確かめられてしまう。
    """
    response = _change_my_password(
        client, "99999", ROLE_EMPLOYEE, current=初期パスワード, new="newpass123"
    )

    assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    ("新しいパスワード", "説明"),
    [
        ("a" * (MIN_PASSWORD_LENGTH - 1), "短すぎる"),
        ("a" * (MAX_PASSWORD_BYTES + 1), "長すぎる"),
        ("あ" * 25, "日本語25文字=75バイトで上限超え"),
        ("", "空"),
    ],
)
def test_新しいパスワードの長さが規定外なら400(
    client: TestClient, temp_db: None, 新しいパスワード: str, 説明: str
) -> None:
    """保存できない長さのパスワードを本人に決めさせない。

    上限を通してしまうと hash_password() の中で ValueError になって500になる。
    """
    user_id, employee_code, role = EMPLOYEE

    response = _change_my_password(
        client, user_id, role, current=初期パスワード, new=新しいパスワード
    )

    assert response.status_code == 400, f"{説明}: {response.text}"
    # 弾かれたのだから、元のパスワードのまま
    assert _login(client, employee_code, 初期パスワード).json()["success"] is True


def test_新旧が同じパスワードでも通る(client: TestClient, temp_db: None) -> None:
    """Issue #123 で「既存と同じパスワードの禁止はしない」と決めている。"""
    user_id, employee_code, role = EMPLOYEE

    response = _change_my_password(
        client, user_id, role, current=初期パスワード, new=初期パスワード
    )

    assert response.status_code == 200, response.text
    assert _login(client, employee_code, 初期パスワード).json()["success"] is True


def test_未ログインでは自分のパスワードを変更できない(client: TestClient, temp_db: None) -> None:
    """Authorization ヘッダが無ければ、そもそも本人確認まで進まない。"""
    response = client.put(
        "/api/me/password",
        json={"current_password": 初期パスワード, "new_password": "newpass123"},
    )

    assert response.status_code in (401, 403), response.text


# --- B. システム管理者による強制上書き ---------------------------------------------


def test_システム管理者は他人のパスワードを上書きできる(client: TestClient, temp_db: None) -> None:
    """本人が忘れたときに復旧させるための入口。現在のパスワードは要らない。"""
    対象user_id, employee_code, _ = EMPLOYEE

    response = _reset_password(client, 対象user_id, "resetpass123")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert response.json()["user_id"] == 対象user_id


def test_上書き後はそのパスワードでログインできる(client: TestClient, temp_db: None) -> None:
    """ここが本命。上書きが実際に効いていることを確かめる。"""
    対象user_id, employee_code, _ = EMPLOYEE

    _reset_password(client, 対象user_id, "resetpass123")

    assert _login(client, employee_code, "resetpass123").json()["success"] is True
    assert _login(client, employee_code, 初期パスワード).json()["success"] is False


def test_本人が変更したあとでも上書きできる(client: TestClient, temp_db: None) -> None:
    """パスワードを忘れた人を助ける、という本来の使い方。

    本人が変更してハッシュが入っている状態でも、上書きできる必要がある。
    """
    対象user_id, employee_code, role = EMPLOYEE
    _change_my_password(client, 対象user_id, role, current=初期パスワード, new="mypassword1")

    _reset_password(client, 対象user_id, "resetpass123")

    assert _login(client, employee_code, "resetpass123").json()["success"] is True
    assert _login(client, employee_code, "mypassword1").json()["success"] is False


def test_システム管理者は自分のパスワードも上書きできる(client: TestClient, temp_db: None) -> None:
    """禁止する理由がない（ロール変更と違い、社長ゼロのような事故が起きない）。"""
    対象user_id, employee_code, _ = ADMIN

    response = _reset_password(client, 対象user_id, "adminpass123")

    assert response.status_code == 200, response.text
    assert _login(client, employee_code, "adminpass123").json()["success"] is True


def test_社長のパスワードも上書きできる(client: TestClient, temp_db: None) -> None:
    """社員データ画面と違い、ceo を404で除外しない。

    社長のパスワードを復旧できないと、アカウント管理の機能として成り立たない。
    """
    対象user_id, employee_code, _ = CEO

    response = _reset_password(client, 対象user_id, "ceopass12345")

    assert response.status_code == 200, response.text
    assert _login(client, employee_code, "ceopass12345").json()["success"] is True


@pytest.mark.parametrize(("user_id", "employee_code", "role"), [EMPLOYEE, SOURCE_MANAGER, CEO])
def test_システム管理者以外は他人のパスワードを上書きできない(
    client: TestClient, temp_db: None, user_id: str, employee_code: str, role: str
) -> None:
    """社長(ceo)も弾かれる。アカウント管理は admin だけの仕事（CLAUDE.md の権限表）。"""
    対象user_id, 対象employee_code, _ = EMPLOYEE

    response = _reset_password(
        client, 対象user_id, "resetpass123", 叩く人=(user_id, employee_code, role)
    )

    assert response.status_code == 403, response.text
    # 弾かれたのだから、対象のパスワードは変わっていない
    assert _login(client, 対象employee_code, 初期パスワード).json()["success"] is True


def test_未ログインでは上書きできない(client: TestClient, temp_db: None) -> None:
    """Authorization ヘッダが無ければ、そもそも権限判定まで進まない。"""
    response = client.put(
        "/api/admin/accounts/2/password",
        json={"new_password": "resetpass123"},
    )

    assert response.status_code in (401, 403), response.text


def test_存在しない社員は404(client: TestClient, temp_db: None) -> None:
    """user_passwords に、誰のものでもない行が増えるのを防ぐ。"""
    response = _reset_password(client, "99999", "resetpass123")

    assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    ("新しいパスワード", "説明"),
    [
        ("a" * (MIN_PASSWORD_LENGTH - 1), "短すぎる"),
        ("a" * (MAX_PASSWORD_BYTES + 1), "長すぎる"),
        ("あ" * 25, "日本語25文字=75バイトで上限超え"),
        ("", "空"),
    ],
)
def test_上書きでも長さが規定外なら400(
    client: TestClient, temp_db: None, 新しいパスワード: str, 説明: str
) -> None:
    """アカウント追加・本人による変更と同じ規則で弾かれる。"""
    対象user_id, employee_code, _ = EMPLOYEE

    response = _reset_password(client, 対象user_id, 新しいパスワード)

    assert response.status_code == 400, f"{説明}: {response.text}"
    # 弾かれたのだから、元のパスワードのまま
    assert _login(client, employee_code, 初期パスワード).json()["success"] is True


# --- C. 3つのAPIで長さの規則と文言が揃っていること ---------------------------------


def test_3つのAPIが同じ文言で長さを拒否する(client: TestClient, temp_db: None) -> None:
    """検証を app/user_passwords.py に切り出した目的そのものの確認。

    片方だけ規則が変わると、利用者から見て「どこで何文字必要なのか」が
    画面ごとに食い違う。1箇所に集約してあることを、応答の一致で確かめる。
    """
    短すぎる = "a" * (MIN_PASSWORD_LENGTH - 1)
    対象user_id, _, role = EMPLOYEE

    追加 = client.post(
        "/api/admin/accounts",
        json={
            "employee_code": "EMP008",
            "name": "新入社員",
            "role": ROLE_EMPLOYEE,
            "password": 短すぎる,
        },
        headers=_headers(ADMIN[0], ADMIN[2]),
    )
    本人変更 = _change_my_password(client, 対象user_id, role, current=初期パスワード, new=短すぎる)
    強制上書き = _reset_password(client, 対象user_id, 短すぎる)

    assert 追加.status_code == 400
    assert 本人変更.status_code == 400
    assert 強制上書き.status_code == 400
    assert 追加.json()["detail"] == 本人変更.json()["detail"] == 強制上書き.json()["detail"]
