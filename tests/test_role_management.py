"""ロール変更（user_roles テーブル と PUT /api/admin/users/{user_id}/role）のテスト。

何を守りたいか:
    1. ロールの上書きが「1社員1行」で正しく積まれること（UPSERT が効いていること）
    2. ロール変更は社長だけができること。社員も source_manager も叩けない
    3. 知らないロール名や実在しない社員を保存させないこと
       （権限判定が知らない値がDBに入ると、その社員はどの権限にも当てはまらなくなる）
    4. 社長が自分自身のロールを変えられないこと（管理者ゼロの事故を防ぐ）
    5. 変更したロールが、次回ログインで実際に効くこと
       （レスポンスの role と JWT の role が食い違わないこと）

DBを使う理由:
    tests/test_source_permission.py は「拒否されること」だけを見るのでDB不要だったが、
    こちらは保存された値そのものを確かめるため、テスト用PostgreSQLが要る。
    DBに触るテストには temp_db フィクスチャを付けている。

users.csv 上の前提:
    user_id=1 … ADMIN（ceo）
    user_id=2 … EMP001（employee）
    user_id=8 … EMP007（source_manager）
"""

import jwt
import pytest
from fastapi.testclient import TestClient

from app import config, database
from app.main import app
from app.routers.auth_router import (
    ROLE_CEO,
    ROLE_EMPLOYEE,
    ROLE_SOURCE_MANAGER,
    create_access_token,
)
from app.user_logins import get_last_login_at
from app.user_roles import get_role, set_role

# テストで使う社員の user_id（users.csv の並びに対応する）
ADMIN_USER_ID = "1"
EMPLOYEE_USER_ID = "2"


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    入力:
        user_id … トークンに載せる user_id
        role    … トークンに載せるロール名

    出力:
        {"Authorization": "Bearer <JWT>"} の形の辞書

    なぜ dependency_overrides ではなく本物のトークンを使うか:
        このファイルの検証対象には権限判定そのものが含まれる。
        差し替えてしまうと、確かめたい処理を飛ばしてしまう。
        また、自分自身かどうかの判定は token["user_id"] を見るため、
        トークンに載せる user_id をテストごとに変えられる必要がある。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


def _fetch_role_rows(user_id: str) -> list[dict]:
    """user_roles テーブルから、指定 user_id の行をすべて取り出す。

    行数も確かめたいので1件取得ではなく全件を返す
    （UPSERT が効かず行が増えていないかを見るため）。
    """
    conn = database.get_connection()
    rows = conn.execute(
        "SELECT user_id, role, updated_at, updated_by FROM user_roles WHERE user_id = %s",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# --- A. app/user_roles.py の get_role / set_role -------------------------------


def test_上書きが無ければNoneを返す(temp_db: None) -> None:
    """まだ一度もロールを変更していない社員は、行そのものが無い。

    None は「ロールが無い」ではなく「上書きされていない」の意味で、
    呼び出し元（resolve_role）はこのとき users.csv の role を使う。
    """
    assert get_role(EMPLOYEE_USER_ID) is None


def test_保存したロールが取り出せる(temp_db: None) -> None:
    """set_role で保存した値が、そのまま get_role で返る。"""
    set_role(EMPLOYEE_USER_ID, ROLE_SOURCE_MANAGER, updated_by=ADMIN_USER_ID)

    assert get_role(EMPLOYEE_USER_ID) == ROLE_SOURCE_MANAGER


def test_同じ社員に2回保存しても行が増えない(temp_db: None) -> None:
    """UPSERT（ON CONFLICT DO UPDATE）が効いていることを固定する。

    「消してから入れ直す」方式や、行を足していく方式にしてしまうと、
    どれが最新か分からなくなる。user_id を主キーにして1社員1行を保つ。
    """
    set_role(EMPLOYEE_USER_ID, ROLE_SOURCE_MANAGER, updated_by=ADMIN_USER_ID)
    set_role(EMPLOYEE_USER_ID, ROLE_EMPLOYEE, updated_by=ADMIN_USER_ID)

    rows = _fetch_role_rows(EMPLOYEE_USER_ID)

    # 行は1つのまま、中身だけが2回目の値に置き換わっている
    assert len(rows) == 1
    assert rows[0]["role"] == ROLE_EMPLOYEE
    assert get_role(EMPLOYEE_USER_ID) == ROLE_EMPLOYEE


# --- B. PUT /api/admin/users/{user_id}/role ------------------------------------


def _update_role(client: TestClient, target_user_id: str, role: str, headers: dict[str, str]):
    """ロール変更APIを叩く（テスト用の短縮形）。"""
    return client.put(
        f"/api/admin/users/{target_user_id}/role",
        json={"role": role},
        headers=headers,
    )


def test_管理者は社員のロールを変更できる(temp_db: None) -> None:
    """正常系。200が返り、DBにも変更後の値が保存されている。"""
    response = _update_role(
        TestClient(app),
        EMPLOYEE_USER_ID,
        ROLE_SOURCE_MANAGER,
        _headers(ADMIN_USER_ID, ROLE_CEO),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True,
        "user_id": EMPLOYEE_USER_ID,
        "role": ROLE_SOURCE_MANAGER,
    }

    # レスポンスだけでなく、DBに実際に積まれているかまで確かめる
    rows = _fetch_role_rows(EMPLOYEE_USER_ID)
    assert len(rows) == 1
    assert rows[0]["role"] == ROLE_SOURCE_MANAGER

    # 誰が変えたかも残っている（監査のため updated_by を持たせている）
    assert rows[0]["updated_by"] == ADMIN_USER_ID


def test_知らないロール名は400(temp_db: None) -> None:
    """権限判定が知らない値をDBに入れさせない。

    ここを通してしまうと、その社員は「どの権限にも当てはまらない状態」で
    ログインすることになる。
    """
    response = _update_role(
        TestClient(app), EMPLOYEE_USER_ID, "manager", _headers(ADMIN_USER_ID, ROLE_CEO)
    )

    assert response.status_code == 400, response.text

    # 弾かれた以上、DBには何も積まれていないこと
    assert _fetch_role_rows(EMPLOYEE_USER_ID) == []


def test_存在しない社員は404(temp_db: None) -> None:
    """実在しない user_id を指定しても、user_roles に幽霊の行が増えない。"""
    response = _update_role(
        TestClient(app), "99999", ROLE_SOURCE_MANAGER, _headers(ADMIN_USER_ID, ROLE_CEO)
    )

    assert response.status_code == 404, response.text
    assert _fetch_role_rows("99999") == []


@pytest.mark.parametrize("変更後のロール", [ROLE_EMPLOYEE, ROLE_CEO])
def test_自分自身のロールは変更できない(変更後のロール: str, temp_db: None) -> None:
    """社長が自分を対象にすると403。変更先が ceo であっても一律で拒否する。

    最後の管理者が自分を employee に落とすと、誰もこのAPIを叩けなくなり、
    ロールを戻す手段がDBの直接操作しか無くなる（管理者ゼロの事故）。
    「ceo → ceo なら実質何も変わらないので許す」といった例外を作らないのは、
    条件が増えるほど事故を防げているかの確認が難しくなるため。
    """
    response = _update_role(
        TestClient(app), ADMIN_USER_ID, 変更後のロール, _headers(ADMIN_USER_ID, ROLE_CEO)
    )

    assert response.status_code == 403, response.text
    assert _fetch_role_rows(ADMIN_USER_ID) == []


@pytest.mark.parametrize("role", [ROLE_EMPLOYEE, ROLE_SOURCE_MANAGER])
def test_管理者以外はロールを変更できない(role: str, temp_db: None) -> None:
    """社員も source_manager も403（require_ceo が効いている）。

    ロールの付け替えは「誰が何を見られるか」を決める操作なので、
    共通ソースを登録できるだけの source_manager にも許さない。
    """
    response = _update_role(TestClient(app), EMPLOYEE_USER_ID, ROLE_CEO, _headers("8", role))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "管理者のみ操作できます"

    # 弾かれた以上、DBには何も積まれていないこと
    assert _fetch_role_rows(EMPLOYEE_USER_ID) == []


# --- C. ログイン時の実効ロール反映 ---------------------------------------------


def test_変更したロールが次回ログインで反映される(temp_db: None) -> None:
    """set_role で変更したロールが、ログインのレスポンスとJWTの両方に載る。

    レスポンスの role は画面の遷移先の判定に使われ、JWTの role はAPI側の
    権限判定に使われる。この2つが食い違うと、管理者画面へ遷移したのに
    APIが403を返す、という噛み合わない状態になるため、必ず同じ値であることを固定する。

    「次回ログインから有効」なのは、ロールがログイン時にJWTへ焼き付けられ、
    発行後は書き換えられないため（app/routers/auth_router.py の login を参照）。
    """
    # EMP001（users.csv では employee）を source_manager に変更しておく
    set_role(EMPLOYEE_USER_ID, ROLE_SOURCE_MANAGER, updated_by=ADMIN_USER_ID)

    response = TestClient(app).post(
        "/api/login",
        json={"employee_code": "EMP001", "password": "password"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True

    # CSVの employee ではなく、DBで上書きした source_manager が返る
    assert body["role"] == ROLE_SOURCE_MANAGER

    # トークンの中身も同じロールであること（画面とAPIで食い違わせない）
    payload = jwt.decode(body["token"], config.JWT_SECRET_KEY, algorithms=[config.JWT_ALGORITHM])
    assert payload["role"] == ROLE_SOURCE_MANAGER
    assert payload["user_id"] == EMPLOYEE_USER_ID


# --- D. 実効ロールの検証（未知のロール名を拒否する）-----------------------------


def _login(employee_code: str, password: str = "password"):
    """ログインAPIを叩く（テスト用の短縮形）。"""
    return TestClient(app).post(
        "/api/login",
        json={"employee_code": employee_code, "password": password},
    )


def test_未知のロールが記録された社員はログインできない(temp_db: None) -> None:
    """user_roles に VALID_ROLES に無い値が入っていたら、ログインを拒否する。

    なぜこのテストが必要か:
        ロール名を改名したのに古い行が残っている、DBを直接書き換えた、といった場合に、
        権限判定が知らない値がJWTへ焼き付けられてしまう。
        そのユーザーは「どの権限にも当てはまらない状態」でログインでき、
        以後の挙動が読めなくなる。入口で止めることを固定する。

    token と role を返さないことを確かめる理由:
        ここが要点。認証できていない相手にトークンやロールを渡していないことを見る。
        返してしまうと、不正な状態のまま以降のAPIを叩ける経路が残る。

    message を認証失敗と同一にする理由:
        「ロールが不正です」と伝えると、内部の状態を外に漏らすことになる。
        原因はサーバーのログ（logger.error）から追う。

    最終ログインが更新されないことも確かめる理由:
        record_login() が記録するのは「最後に入れたのはいつか」を示す値なので、
        ロール検証で拒否した相手の記録を作ってはいけない。
        作ってしまうと、社員データ画面に「実際には入れていない時刻」が
        最終ログインとして並んでしまう。
    """
    # set_role は渡された値をそのまま保存する（妥当性の判断は呼び出し側の責任）。
    # ここではAPIを通さず直接記録し、「DBに不正な値がある状態」を作る
    set_role(EMPLOYEE_USER_ID, "manager", updated_by=ADMIN_USER_ID)

    response = _login("EMP001")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["success"] is False
    # 認証できていない相手には、トークンもロールも渡さない
    assert "token" not in body
    assert "role" not in body
    # どこで弾かれたかを外に漏らさないため、文言は通常の認証失敗と同じ
    assert body["message"] == "社員コードまたはパスワードが正しくありません"

    # 入れていない以上、最終ログインも記録されない
    # （temp_db で user_logins は空から始まるので、None のままなら記録されていない）
    assert get_last_login_at(EMPLOYEE_USER_ID) is None


def test_正常なロールが記録された社員はログインできる(temp_db: None) -> None:
    """検証を足したことで、正しいロールのログインまで塞いでいないことを確かめる。

    拒否側だけを固定すると、条件を厳しくしすぎたときに気づけない。
    通る側も一緒に固定しておく。
    """
    set_role(EMPLOYEE_USER_ID, ROLE_SOURCE_MANAGER, updated_by=ADMIN_USER_ID)

    response = _login("EMP001")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["success"] is True
    assert body["role"] == ROLE_SOURCE_MANAGER
    assert body["token"]
