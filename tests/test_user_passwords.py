"""パスワードのハッシュ化（app/user_passwords.py と POST /api/login）のテスト。

対象:
    app/user_passwords.py … ハッシュ化・照合・user_passwords テーブルの読み書き
    POST /api/login       … ログイン時の照合と、CSVからハッシュへの移行

何を守りたいか:
    1. 平文がそのまま保存されないこと（ハッシュ化した意味が失われないこと）
    2. ソルトが毎回変わること。同じパスワードでも保存値が一致しないこと
       ＝ 保存値を見比べて「この2人は同じパスワード」と分かる状態にしない
    3. 壊れた値がDBに入っていても、例外ではなく「照合失敗」として扱われること
       ＝ 1件の不正な行でログイン処理全体が落ちないこと
    4. 移行がログインを引き金に、正しいパスワードのときだけ行われること
       ＝ 間違ったパスワードを保存すると、その値でログインできてしまう
    5. 移行後も同じパスワードでログインできること（2回目はDBのハッシュ経由になる）
    6. ハッシュの保存に失敗しても、認証に通った社員はログインできること

users.csv 上の前提:
    user_id=2 … EMP001（employee、パスワードは "password"）
    user_id=3 … EMP002（employee）
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.user_passwords import (
    get_password_hash,
    hash_password,
    set_password,
    verify_password,
)

# テストで使う社員の user_id（users.csv の並びに対応する）
EMPLOYEE_USER_ID = "2"

# data/users.csv に入っている検証用のパスワード
CSV_PASSWORD = "password"


@pytest.fixture
def client(temp_db: None) -> Iterator[TestClient]:
    """空のDBに対してログインAPIを叩けるクライアント。

    ログインAPI（POST /api/login）は認証不要なので、
    dependency_overrides での差し替えは何もしない。
    確かめたいのは照合と移行そのものなので、本物の処理を通す。
    """
    yield TestClient(app)


def _login(client: TestClient, employee_code: str, password: str = CSV_PASSWORD) -> httpx.Response:
    """ログインAPIを叩く（テスト用の短縮形）。"""
    return client.post(
        "/api/login",
        json={"employee_code": employee_code, "password": password},
    )


def _fetch_all_passwords() -> dict[str, str]:
    """user_passwords テーブルの中身を {user_id: password_hash} で取り出す。"""
    conn = database.get_connection()
    rows = conn.execute("SELECT user_id, password_hash FROM user_passwords").fetchall()
    conn.close()
    return {row["user_id"]: row["password_hash"] for row in rows}


# --- ハッシュ化・照合（DBを使わない） ---------------------------------------


def test_同じパスワードでもハッシュ化のたびに値が変わる() -> None:
    """gensalt() が毎回違うソルトを作るため、保存値は一致しない。

    ここが同じ値になると、保存値を見比べるだけで
    「この2人は同じパスワードを使っている」と分かってしまう。
    """
    first = hash_password(CSV_PASSWORD)
    second = hash_password(CSV_PASSWORD)

    assert first != second


def test_ハッシュ化した値は元のパスワードと一致しない() -> None:
    """平文がそのまま返っていないこと。ハッシュ化の最低条件。"""
    hashed = hash_password(CSV_PASSWORD)

    assert hashed != CSV_PASSWORD
    assert CSV_PASSWORD not in hashed


def test_正しいパスワードなら照合が通る() -> None:
    """毎回ソルトが変わっても、元のパスワードなら照合できる。

    ハッシュ文字列の中にソルトが埋め込まれており、
    checkpw がそれを取り出して同じ条件で計算し直すため。
    """
    hashed = hash_password(CSV_PASSWORD)

    assert verify_password(CSV_PASSWORD, hashed) is True


def test_違うパスワードでは照合が通らない() -> None:
    """1文字でも違えば False。"""
    hashed = hash_password(CSV_PASSWORD)

    assert verify_password("wrong-password", hashed) is False


@pytest.mark.parametrize(
    ("password_hash", "理由"),
    [
        ("", "空文字（行はあるが値が入っていない）"),
        (CSV_PASSWORD, "平文がそのまま入っていた（移行前の値が紛れ込んだ）"),
    ],
)
def test_bcryptの形式でない値を渡しても例外にならずFalseが返る(
    password_hash: str, 理由: str
) -> None:
    """壊れた値がDBに1件あるだけでログイン処理全体が落ちないようにする。

    bcrypt.checkpw は形式が不正だと ValueError を投げる。
    これを外に出すと500エラーになるため、
    verify_password が捕まえて「照合失敗」に倒している。
    """
    assert verify_password(CSV_PASSWORD, password_hash) is False, 理由


# --- DB操作（temp_db を使う） -----------------------------------------------


def test_記録が無い社員のget_password_hashはNone(temp_db: None) -> None:
    """DB層は「まだ設定されていない」を None で返す。

    None のときCSVの平文を使うかどうかを決めるのは呼び出し元（auth_router）の責任。
    """
    assert get_password_hash(EMPLOYEE_USER_ID) is None


def test_set_passwordで保存した値がget_password_hashで取り出せる(temp_db: None) -> None:
    """書いた値がそのまま読み出せること。取り出した値で照合も通る。"""
    set_password(EMPLOYEE_USER_ID, CSV_PASSWORD)

    saved = get_password_hash(EMPLOYEE_USER_ID)

    assert saved is not None
    assert verify_password(CSV_PASSWORD, saved) is True


def test_保存されるのは平文ではない(temp_db: None) -> None:
    """set_password に渡した平文が、そのままDBへ入っていないこと。"""
    set_password(EMPLOYEE_USER_ID, CSV_PASSWORD)

    saved = get_password_hash(EMPLOYEE_USER_ID)

    assert saved != CSV_PASSWORD


def test_同じ社員に2回set_passwordすると上書きされる(temp_db: None) -> None:
    """UPSERT なので行は増えず、新しいパスワードだけが有効になる。

    古いパスワードで通ってしまうと、パスワード変更が変更になっていない。
    """
    set_password(EMPLOYEE_USER_ID, CSV_PASSWORD)
    set_password(EMPLOYEE_USER_ID, "new-password")

    saved = get_password_hash(EMPLOYEE_USER_ID)

    assert saved is not None
    assert list(_fetch_all_passwords().keys()) == [EMPLOYEE_USER_ID]  # 行は1つのまま
    assert verify_password("new-password", saved) is True
    assert verify_password(CSV_PASSWORD, saved) is False


# --- ログイン経由の移行（temp_db を使う） -----------------------------------


def test_初回ログイン成功でハッシュが保存される(client: TestClient) -> None:
    """CSVの平文で認証が通った社員は、その場でDBのハッシュへ移行される。

    専用の移行スクリプトを作らず、ログインを唯一の移行経路にしているため、
    ここが動かないと誰もハッシュに移らない。
    """
    response = _login(client, "EMP001")

    assert response.json()["success"] is True
    assert list(_fetch_all_passwords().keys()) == [EMPLOYEE_USER_ID]


def test_ログインで保存された値は平文ではない(client: TestClient) -> None:
    """移行経路を通っても平文がDBに入らないこと。"""
    _login(client, "EMP001")

    assert _fetch_all_passwords()[EMPLOYEE_USER_ID] != CSV_PASSWORD


def test_移行後も同じパスワードでログインできる(client: TestClient) -> None:
    """2回目はDBのハッシュ経由（CSVを見ない）で照合され、結果は変わらない。

    ここが落ちると、一度ログインした社員が次から入れなくなる。
    """
    first = _login(client, "EMP001")
    saved = _fetch_all_passwords()[EMPLOYEE_USER_ID]

    second = _login(client, "EMP001")

    assert first.json()["success"] is True
    assert second.json()["success"] is True
    # 2回目は保存済みのハッシュを使うだけで、上書きは起きない
    assert _fetch_all_passwords()[EMPLOYEE_USER_ID] == saved


@pytest.mark.parametrize(
    ("employee_code", "password", "理由"),
    [
        ("EMP001", "wrong-password", "パスワードが違う"),
        ("EMP999", CSV_PASSWORD, "存在しない社員コード"),
    ],
)
def test_ログイン失敗では保存されない(
    client: TestClient, employee_code: str, password: str, 理由: str
) -> None:
    """間違ったパスワードを保存すると、その値で本人としてログインできてしまう。"""
    response = _login(client, employee_code, password)

    assert response.json()["success"] is False, 理由
    assert _fetch_all_passwords() == {}, 理由


def test_ハッシュの保存に失敗してもログインは成功する(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """移行はログインの副次的な処理なので、失敗しても認証結果を覆さない。

    DBが一時的に書けないというだけで、正しいパスワードを入れた社員を
    締め出すのは筋が違う。保存できなかった社員は次回ログインで再度移行される。
    （チャット履歴の保存に失敗しても回答は返す chat_router.py と同じ考え方）
    """

    def _失敗する(user_id: str, password: str) -> None:
        raise RuntimeError("DBに書き込めない状況を再現する")

    monkeypatch.setattr("app.routers.auth_router.set_password", _失敗する)

    response = _login(client, "EMP001")

    assert response.json()["success"] is True
    assert _fetch_all_passwords() == {}  # 保存は行われていない
