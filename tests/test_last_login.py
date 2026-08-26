"""最終ログイン日時（user_logins テーブル）のテスト。

対象:
    POST /api/login                 … ログイン成功時に日時を記録する
    GET  /api/admin/users/{user_id} … 社員データ画面が記録された日時を返す

何を守りたいか:
    1. ログイン成功時だけ記録され、失敗時には記録されないこと
    2. ある社員のログインで、他の社員の記録が書き換わらないこと（UPSERTの1行更新）
    3. 同じ社員が何度ログインしても1行のまま上書きされること
    4. 日時の形式が既存（chat_sessions.started_at 等）と揃っていること
       ＝ UTC・ISO形式・オフセット付き。ここがズレると画面の表示時刻が9時間ずれる
    5. 記録が無い社員は空文字が返り、画面が「未記録」と表示できること
    6. ログインしても data/users.csv が書き換わらないこと
       ＝ 記録先をDBに分けた理由そのもの。CSVはGit管理下なので変わると差分が出る
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import database, user_logins
from app.main import app
from app.routers.auth_router import require_ceo
from app.users import USERS_CSV_PATH


@pytest.fixture
def client(temp_db: None) -> Iterator[TestClient]:
    """管理者としてログイン済みの状態でAPIを叩けるクライアント。

    ログインAPI（POST /api/login）は認証不要なので、この差し替えの影響を受けない。
    差し替えているのは社員データ画面API（require_ceo）の方だけ。
    """
    app.dependency_overrides[require_ceo] = lambda: {"user_id": "1", "role": "ceo"}
    yield TestClient(app)
    app.dependency_overrides.clear()


def _login(client: TestClient, employee_code: str, password: str = "password") -> httpx.Response:
    """ログインAPIを叩く（テスト用の短縮形）。"""
    return client.post(
        "/api/login",
        json={"employee_code": employee_code, "password": password},
    )


def _fetch_all_logins() -> dict[str, str]:
    """user_logins テーブルの中身を {user_id: last_login_at} で取り出す。"""
    conn = database.get_connection()
    rows = conn.execute("SELECT user_id, last_login_at FROM user_logins").fetchall()
    conn.close()
    return {row["user_id"]: row["last_login_at"] for row in rows}


def test_ログイン成功で最終ログインが記録される(client: TestClient) -> None:
    """EMP001（user_id=2）でログインすると、その1行だけが作られる。"""
    response = _login(client, "EMP001")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert list(_fetch_all_logins().keys()) == ["2"]


def test_記録された日時はUTCのISO形式(client: TestClient) -> None:
    """既存の started_at / uploaded_at と同じ形式（UTC・オフセット付きISO）で保存される。

    ここがズレると、画面側の new Date() がローカル時刻と誤解して
    「最終ログイン」だけ9時間ずれて表示される。
    """
    _login(client, "EMP001")

    saved = _fetch_all_logins()["2"]
    parsed = datetime.fromisoformat(saved)

    # オフセットを持っていること（naive な文字列ではないこと）
    assert parsed.tzinfo is not None
    # UTCで保存されていること（オフセットが ±0）
    assert parsed.utcoffset() == timedelta(0)
    # 「今」が入っていること（前後1分以内）
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60


def test_他の社員でログインしても既存の記録は変わらない(client: TestClient) -> None:
    """Issue #54 の完了条件。EMP002 のログインで EMP001 の記録が壊れないこと。"""
    _login(client, "EMP001")
    emp001_before = _fetch_all_logins()["2"]

    _login(client, "EMP002")

    logins = _fetch_all_logins()
    assert logins["2"] == emp001_before  # EMP001 は手つかず
    assert "3" in logins  # EMP002 が新しく記録された


def test_同じ社員が2回ログインすると1行のまま上書きされる(client: TestClient) -> None:
    """UPSERT なので行は増えず、日時だけが新しくなる。"""
    _login(client, "EMP001")
    first = _fetch_all_logins()["2"]

    _login(client, "EMP001")
    logins = _fetch_all_logins()

    assert list(logins.keys()) == ["2"]  # 行は1つのまま
    assert logins["2"] >= first  # 日時は同じか新しい（ISO形式なので文字列比較で順序が保てる）


def test_ログインしてもusers_csvは1バイトも変わらない(client: TestClient) -> None:
    """Issue #54 の完了条件。社員マスタCSVは読み取り専用のままであること。

    なぜバイト列で比べるか:
        「差分が出ない」を人の目で確認するのではなく、テストで固定するため。
        将来うっかりCSVへ書き戻す実装に戻したり、CSVを読むついでに
        書き直すようなコードが入ったりすると、ここで落ちる。

    data/users.csv はGit管理下にあるため、ログインのたびに中身が変わると
    `git status` に差分が出続け、コミットのたびにノイズになる。
    """
    before = USERS_CSV_PATH.read_bytes()

    _login(client, "EMP001")
    _login(client, "EMP002")
    _login(client, "EMP001", "wrong-password")

    assert USERS_CSV_PATH.read_bytes() == before


@pytest.mark.parametrize(
    ("employee_code", "password", "理由"),
    [
        ("EMP001", "wrong-password", "パスワードが違う"),
        ("EMP999", "password", "存在しない社員コード"),
        ("", "password", "社員コードが空"),
    ],
)
def test_ログイン失敗では記録されない(
    client: TestClient, employee_code: str, password: str, 理由: str
) -> None:
    """失敗も記録すると、入れていない試行が「最終ログイン」に見えてしまう。"""
    response = _login(client, employee_code, password)

    assert response.json()["success"] is False, 理由
    assert _fetch_all_logins() == {}, 理由


def test_社員データ画面に記録された日時が返る(client: TestClient) -> None:
    """ログイン → 社員データ画面API、の順で日時がつながることを確認する。"""
    _login(client, "EMP001")
    saved = _fetch_all_logins()["2"]

    response = client.get("/api/admin/users/2")

    assert response.status_code == 200, response.text
    assert response.json()["last_login_at"] == saved


def test_一度もログインしていない社員は空文字が返る(client: TestClient) -> None:
    """記録が無い社員は空文字。画面側（formatLastLogin）が「未記録」と表示する。"""
    response = client.get("/api/admin/users/2")

    assert response.status_code == 200
    assert response.json()["last_login_at"] == ""


def test_記録が無い社員のget_last_login_atはNone(temp_db: None) -> None:
    """DB層は「記録が無い」を None で返す（空文字に変えるのは呼び出し元の責任）。"""
    assert user_logins.get_last_login_at("2") is None


def test_record_loginは記録した日時を返す(temp_db: None) -> None:
    """戻り値と、実際にDBへ入った値が一致する。"""
    recorded = user_logins.record_login("2")

    assert user_logins.get_last_login_at("2") == recorded
