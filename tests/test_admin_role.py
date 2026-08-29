"""システム管理者ロール（admin）の権限テスト（Issue #122）。

admin はどういう役割か:
    アカウントの管理だけを担当し、業務データ（チャット・ソース・社員データ）は
    一切持たない・触らない役割。社長(ceo)の「全部見られる」とは正反対で、
    「業務については何も見られない」のが正しい状態。

何を守りたいか:
    1. admin が業務系エンドポイント（チャット・自分の資料の登録）で403になること
       … 入口の require_business_user が弾く
    2. admin が社長専用エンドポイントで403になること
       … 入口の require_ceo が弾く（admin は ceo ではない）
    3. admin が共通ソース管理者向けエンドポイントで403になること
       … 入口の require_source_uploader が弾く
    4. admin がスタッフ一覧に並ばず、詳細も引けず、
       社員別チャットの対象にもできないこと
       … STAFF_LIST_EXCLUDED_ROLES による除外
    5. 既存3ロール（employee / source_manager / ceo）の権限が変わっていないこと
       … 締め出しを足したことで、通るべき人まで塞いでいないかを見る

なぜ5（既存ロールの確認）を入れるか:
    拒否側だけを固定すると、条件を厳しくしすぎたときに気づけない。
    require_business_user を全エンドポイントに付け違えても、
    admin が403になるテストは全部通ってしまう。
    通る側も一緒に固定して、初めて「admin だけを締め出した」と言える。

DBを使うテストと使わないテストが混在している理由:
    1〜3で確かめたいのは「拒否されること」だけで、拒否はハンドラに入る前
    （FastAPI が依存を解決する段階）に起きる。DBには一度も触らないため temp_db は不要。

    一方4と5は、スタッフ一覧の中身や、通った先のレスポンスを確かめる。
    実効ロールの判定（resolve_role）が user_roles テーブルを引くこともあり、
    こちらは temp_db を付けている。

外部サービスには繋がない:
    「外部サービスを遮断」フィクスチャ（autouse）で、Gemini と Qdrant を呼ぶ関数と、
    ファイルを保存する関数を偽物に差し替えている。
    権限で弾かれる想定のテストはそこへ到達しないが、万一この実装が壊れて
    素通りした場合に、テストが実通信して失敗する（＝原因が分かりにくくなる）のを防ぐ。
    詳しくはフィクスチャの docstring を参照。

users.csv 上の前提:
    user_id=1 … ADMIN（ceo）
    user_id=2 … EMP001（employee）
    user_id=8 … EMP007（source_manager）
    user_id=9 … SYSADMIN（admin）
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app
from app.routers import chat_router, sources_router
from app.routers.auth_router import (
    ROLE_ADMIN,
    ROLE_CEO,
    ROLE_EMPLOYEE,
    ROLE_SOURCE_MANAGER,
    create_access_token,
)
from app.user_roles import set_role
from app.users import get_user_by_id, resolve_role

# テストで使う社員の user_id（users.csv の並びに対応する）
CEO_USER_ID = "1"
EMPLOYEE_USER_ID = "2"
SOURCE_MANAGER_USER_ID = "8"
ADMIN_USER_ID = "9"


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    入力:
        user_id … トークンに載せる user_id
        role    … トークンに載せるロール名

    出力:
        {"Authorization": "Bearer <JWT>"} の形の辞書

    なぜ dependency_overrides ではなく本物のトークンを使うか:
        このファイルの検証対象は権限判定そのもの。
        require_business_user を差し替えてしまうと、確かめたい処理を飛ばしてしまう。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def 外部サービスを遮断(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini・Qdrant・ファイル保存への実通信を、このファイル全体で止める。

    入力: monkeypatch（pytest の差し替え機構）
    出力: なし（副作用として4つの関数が偽物に置き換わる）

    autouse にしている理由:
        このファイルのテストは、ほとんどが「ハンドラに入る前に403で弾かれること」を
        確かめるもの。正しく動いていれば外部サービスには一切到達しない。

        問題は実装が壊れたときで、admin が素通りするとその場で
        Gemini や Qdrant への実通信が始まる。すると失敗の理由が
        「403ではなく200が返った」ではなく「接続エラー」や「APIキーが無い」になり、
        本当の原因（権限の穴）に気づきにくくなる。
        差し替えておけば、壊れたときも「403のはずが200だった」と素直に落ちる。

    何を差し替えるか:
        answer_question        … POST /api/chat が呼ぶ（Gemini + Qdrant）
        answer_question_stream … /stream と /staff-inquiry が呼ぶ（同上）
        _vectorize_and_save    … アップロード系が呼ぶ（Gemini + Qdrant）
        storage.save           … アップロード系が呼ぶ（GCS またはローカルへ書き込む）
    """

    def fake_answer_question(question: str, **kwargs: Any) -> tuple[str, list[str]]:
        return ("（テスト用の回答）", [])

    def fake_answer_question_stream(
        question: str, **kwargs: Any
    ) -> tuple[list[str], Iterator[str]]:
        return ([], iter(["（テスト用の回答）"]))

    def fake_vectorize_and_save(*args: Any, **kwargs: Any) -> int:
        return 0

    def fake_save(save_name: str, contents: bytes) -> str:
        return f"test/{save_name}"

    monkeypatch.setattr(chat_router, "answer_question", fake_answer_question)
    monkeypatch.setattr(chat_router, "answer_question_stream", fake_answer_question_stream)
    monkeypatch.setattr(sources_router, "_vectorize_and_save", fake_vectorize_and_save)
    monkeypatch.setattr(storage, "save", fake_save)


def _テスト用ファイル() -> dict[str, tuple[str, bytes, str]]:
    """アップロード系エンドポイントに添えるダミーファイルを作る。

    ファイルを必ず添えるのは、権限で弾かれたのか、ファイル未添付で422になったのかを
    取り違えないようにするため（tests/test_source_permission.py と同じ理由）。
    """
    return {"file": ("メモ.txt", "これはテスト用の本文です".encode(), "text/plain")}


# --- 前提の確認 -----------------------------------------------------------------


def test_社員マスタにシステム管理者が登録されている(temp_db: None) -> None:
    """users テーブルに admin ロールの SYSADMIN が居ることを先に固定する。

    このファイルの他のテストは、すべて user_id=9 が admin である前提で書いてある。
    行が消えたり role が変わったりすると、以降のテストは
    「admin を締め出せている」ではなく「そもそも admin が居ない」ために通ってしまう。
    前提が崩れたことを、ここで最初に気づけるようにしておく。

    temp_db が要る理由:
        社員マスタは data/users.csv からDBの users テーブルへ移した（Issue #123）。
        get_user_by_id() はDBを引くようになったので、
        テーブルを用意する temp_db（init_db を呼ぶ）が必要になる。
    """
    user = get_user_by_id(ADMIN_USER_ID)

    assert user is not None, "users テーブルに user_id=9 の行がありません"
    assert user["employee_code"] == "SYSADMIN"
    assert user["role"] == ROLE_ADMIN


# --- A. admin は業務系エンドポイントを叩けない -----------------------------------


業務系エンドポイント = [
    ("POST", "/api/chat", {"json": {"question": "就業規則を教えて"}}),
    ("POST", "/api/chat/stream", {"json": {"question": "就業規則を教えて"}}),
    ("POST", "/api/chat/sessions", {"json": {"context_type": "general"}}),
    ("GET", "/api/chat/sessions", {}),
    ("GET", "/api/chat/sessions/1/messages", {}),
    ("POST", "/api/sources/my-upload", {"files": _テスト用ファイル()}),
]


@pytest.mark.parametrize(
    ("method", "path", "リクエスト"),
    業務系エンドポイント,
    ids=[
        "chat",
        "chat-stream",
        "create-session",
        "list-sessions",
        "list-messages",
        "my-upload",
    ],
)
def test_システム管理者は業務系APIを叩けない(
    method: str, path: str, リクエスト: dict[str, Any]
) -> None:
    """admin のトークンでは、チャットも自分の資料の登録も403になる。

    admin はアカウントの管理だけを担当し、業務データを持たない役割なので、
    質問する相手も、登録する資料も存在しない。
    入口の require_business_user が、ハンドラに入る前にまとめて弾く。

    detail まで確かめる理由:
        403にさえなればよいのではなく、「システム管理者だから弾かれた」ことを見たい。
        require_ceo（管理者のみ操作できます）で偶然403になっている場合と
        区別できないと、権限の線引きが変わったことに気づけない。

    存在しない session_id=1 を指定しても404ではなく403が返るのが正しい:
        権限判定はDBを引く前に終わっているので、セッションの有無を教えない。
    """
    response = TestClient(app).request(
        method, path, headers=_headers(ADMIN_USER_ID, ROLE_ADMIN), **リクエスト
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "システム管理者は業務機能を利用できません"


# --- B. admin は社長専用エンドポイントを叩けない ---------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/admin/users"),  # スタッフ一覧
        ("GET", "/api/sources"),  # 全ソース一覧
        ("GET", "/api/sources/1/download"),  # ソースのダウンロード
    ],
)
def test_システム管理者は社長専用APIを叩けない(method: str, path: str) -> None:
    """admin のトークンでは、社長専用の管理機能はすべて403になる。

    「管理者」という言葉が同じでも、admin と ceo は別物であることを固定する。
    admin はアカウントを作る・消す役割で、社員の資料やチャットログを見る役割ではない。

    特にダウンロードが重要:
        中身がそのまま手に入るため、通してしまうと他人の個別ソース
        （評価・給与など）に届く。require_ceo を外していないことを確かめる。

    存在しない source_id=1 を指定しても404ではなく403が返るのが正しい:
        権限判定はDBを引く前に終わっているので、ソースの有無を教えない。
    """
    response = TestClient(app).request(method, path, headers=_headers(ADMIN_USER_ID, ROLE_ADMIN))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "管理者のみ操作できます"


# --- C. admin は共通ソース管理者向けエンドポイントを叩けない ---------------------


def test_システム管理者は共通ソースをアップロードできない() -> None:
    """admin のトークンで POST /api/sources/upload を叩くと403。

    共通ソースは全社員の回答根拠になる。admin に登録を許すと、
    業務を知らない役割が全社の回答内容を左右できてしまう。
    入口の require_source_uploader が ceo / source_manager 以外を弾く。
    """
    response = TestClient(app).post(
        "/api/sources/upload",
        files=_テスト用ファイル(),
        data={"scope": "common"},
        headers=_headers(ADMIN_USER_ID, ROLE_ADMIN),
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "共通ソースをアップロードする権限がありません"


def test_システム管理者は共通ソース一覧を見られない() -> None:
    """admin のトークンで GET /api/sources/common を叩くと403。

    登録できないだけでなく、何が登録されているかも見せない。
    一覧を見せると、社内にどんな資料があるかが分かってしまう。
    """
    response = TestClient(app).get(
        "/api/sources/common", headers=_headers(ADMIN_USER_ID, ROLE_ADMIN)
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "共通ソースをアップロードする権限がありません"


# --- D. スタッフ一覧に admin が並ばない ------------------------------------------


def test_スタッフ一覧にシステム管理者が含まれない(temp_db: None) -> None:
    """社長がスタッフ一覧を開いても、admin のカードは並ばない。

    スタッフ一覧は「業務上の社員」を並べて、その人の資料やトークを追う画面。
    業務データを持たない admin は、並べても開く中身が無い。
    ceo（見ている本人）を除外しているのと同じ理由で除外する。

    user_id と employee_code の両方で確かめる理由:
        このAPIのレスポンスに role は含まれない（画面が使わないため）。
        ロールで直接ふるいにかけられないので、admin である社員を
        user_id と社員コードの2つで特定して、居ないことを確かめる。

    ceo も居ないことを一緒に確かめる理由:
        既存の除外（社長を並べない）を壊していないかを同時に見るため。
        admin を足したときに集合の作り方を間違えると、
        片方だけが漏れる形で壊れうる。
    """
    response = TestClient(app).get("/api/admin/users", headers=_headers(CEO_USER_ID, ROLE_CEO))

    assert response.status_code == 200, response.text
    一覧 = response.json()

    # 一覧が空だと「除外できている」ようにも見えてしまうので、中身があることを先に確かめる
    assert len(一覧) > 0

    user_ids = [staff["user_id"] for staff in 一覧]
    employee_codes = [staff["employee_code"] for staff in 一覧]

    # システム管理者（今回追加した除外）
    assert ADMIN_USER_ID not in user_ids
    assert "SYSADMIN" not in employee_codes

    # 社長（従来からの除外。壊れていないことの確認）
    assert CEO_USER_ID not in user_ids

    # 業務上の社員は従来どおり並ぶ（除外を広げすぎていないことの確認）
    assert EMPLOYEE_USER_ID in user_ids
    assert SOURCE_MANAGER_USER_ID in user_ids


# --- E. admin の詳細は引けない ---------------------------------------------------


def test_システム管理者の社員データは404(temp_db: None) -> None:
    """社長が admin の user_id を直接指定しても、詳細は404になる。

    一覧に出ないのに詳細だけ引ける状態にすると、一覧と挙動がずれる。
    URLの user_id を手で書き換えれば開けてしまうため、
    「一覧に出ない人は詳細も見られない」で揃える。

    社員が存在しないときと同じ404にする理由:
        「その人は居るが見せない」と伝えると、
        どの user_id にどんなロールの人が居るかを外に漏らすことになる。
    """
    response = TestClient(app).get(
        f"/api/admin/users/{ADMIN_USER_ID}", headers=_headers(CEO_USER_ID, ROLE_CEO)
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "社員が見つかりません"


def test_社員の社員データは従来どおり引ける(temp_db: None) -> None:
    """除外を足したことで、通常の社員の詳細まで塞いでいないことを確かめる。

    拒否側だけを固定すると、条件を厳しくしすぎたときに気づけない。
    通る側も一緒に固定しておく。
    """
    response = TestClient(app).get(
        f"/api/admin/users/{EMPLOYEE_USER_ID}", headers=_headers(CEO_USER_ID, ROLE_CEO)
    )

    assert response.status_code == 200, response.text
    assert response.json()["employee_code"] == "EMP001"


# --- F. admin を社員別チャットの対象にできない -----------------------------------


def test_システム管理者について質問すると404(temp_db: None) -> None:
    """社長が target_user_id に admin を指定しても404になる。

    黙って通すと、対象の個別ソースが1件も無いまま共通ソースだけで回答が作られる。
    社長には普通の回答に見えてしまい、宛先の間違いに気づけない。
    スタッフ一覧・社員データと同じ集合で除外して、3箇所の見え方を揃えている。
    """
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "この人の評価は？", "target_user_id": ADMIN_USER_ID},
        headers=_headers(CEO_USER_ID, ROLE_CEO),
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "社員が見つかりません"


def test_社員について質問するのは従来どおり通る(temp_db: None) -> None:
    """除外を足したことで、通常の社員への質問まで塞いでいないことを確かめる。

    ここは404にならないことが要点なので、回答の中身までは見ない
    （生成そのものは tests/test_staff_inquiry.py が受け持つ）。
    """
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "この人の評価は？", "target_user_id": EMPLOYEE_USER_ID},
        headers=_headers(CEO_USER_ID, ROLE_CEO),
    )

    assert response.status_code == 200, response.text


# --- G. 既存3ロールの権限が変わっていない ----------------------------------------


def test_社員はチャット履歴を従来どおり見られる(temp_db: None) -> None:
    """employee は GET /api/chat/sessions を従来どおり使える。

    admin を締め出すために require_business_user を足したので、
    社員まで巻き込んで塞いでいないかを確かめる代表例。
    まだ会話が無いので中身は空だが、200が返る（403でない）ことが要点。
    """
    response = TestClient(app).get(
        "/api/chat/sessions", headers=_headers(EMPLOYEE_USER_ID, ROLE_EMPLOYEE)
    )

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_共通ソース管理者は共通ソース一覧を従来どおり見られる(temp_db: None) -> None:
    """source_manager は GET /api/sources/common を従来どおり使える。

    admin と source_manager はどちらも「社長ではない管理系のロール」なので、
    まとめて弾いてしまう壊し方があり得る。区別できていることを確かめる代表例。
    """
    response = TestClient(app).get(
        "/api/sources/common",
        headers=_headers(SOURCE_MANAGER_USER_ID, ROLE_SOURCE_MANAGER),
    )

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_社長はスタッフ一覧を従来どおり見られる(temp_db: None) -> None:
    """ceo は GET /api/admin/users を従来どおり使える。

    社長は業務機能も管理機能も従来どおり全部使える側なので、
    require_business_user が社長を巻き込んでいないことを確かめる代表例。
    """
    response = TestClient(app).get("/api/admin/users", headers=_headers(CEO_USER_ID, ROLE_CEO))

    assert response.status_code == 200, response.text
    assert len(response.json()) > 0


def test_社長は業務機能も従来どおり使える(temp_db: None) -> None:
    """ceo は require_business_user が付いたチャットも従来どおり使える。

    上のスタッフ一覧は require_ceo 側のテストなので、
    今回付け替えた require_business_user 側も社長で1つ確かめておく。
    """
    response = TestClient(app).post(
        "/api/chat/sessions",
        json={"context_type": "general"},
        headers=_headers(CEO_USER_ID, ROLE_CEO),
    )

    assert response.status_code == 200, response.text
    assert "session_id" in response.json()


def test_社員は自分の資料を従来どおり登録できる(temp_db: None) -> None:
    """employee は POST /api/sources/my-upload を従来どおり使える。

    my-upload は今回 verify_token から require_business_user へ付け替えた入口。
    社員が自分の資料を上げる経路を塞いでいないことを確かめる。

    ファイルの保存とベクトル化は「外部サービスを遮断」フィクスチャが
    偽物に差し替えているので、実際のGCSやGeminiには到達しない。
    登録された持ち主が本人になることは tests/test_my_source_upload.py が受け持つ。
    """
    response = TestClient(app).post(
        "/api/sources/my-upload",
        files=_テスト用ファイル(),
        headers=_headers(EMPLOYEE_USER_ID, ROLE_EMPLOYEE),
    )

    assert response.status_code == 200, response.text


# --- H. 実効ロールで判定していること（DBの上書きを見ているか）--------------------


def test_DBでadminにした社員もスタッフ一覧から消える(temp_db: None) -> None:
    """CSVでは employee でも、DBで admin に変更した社員は一覧から消える。

    なぜこのテストが必要か:
        ロールの判定元は2つある（users.csv の値と、user_roles テーブルの上書き）。
        CSVの role を直接見る実装のままだと、DBで admin にした社員が
        一覧に残り続け、締め出しが効いていない人が生まれる。
        一覧・詳細・社員別チャットの3箇所すべてが resolve_role の結果を見ていることを、
        代表として一覧で固定する。

    set_role でDBを直接書き換え、変更APIを使わない理由:
        ここで確かめたいのは変更APIの動作ではなく、
        変更後の値が一覧の判定に効くかどうかのため。
        変更API自体は tests/test_role_management.py が受け持つ。
    """
    # CSVでは employee の EMP001 を、DB側で admin に変更する
    set_role(EMPLOYEE_USER_ID, ROLE_ADMIN, updated_by=CEO_USER_ID)

    # 前提の確認。実効ロールが admin になっている
    user = get_user_by_id(EMPLOYEE_USER_ID)
    assert user is not None
    assert resolve_role(user) == ROLE_ADMIN

    response = TestClient(app).get("/api/admin/users", headers=_headers(CEO_USER_ID, ROLE_CEO))

    assert response.status_code == 200, response.text
    user_ids = [staff["user_id"] for staff in response.json()]

    # CSVの role は employee のままだが、実効ロールが admin なので一覧に出ない
    assert EMPLOYEE_USER_ID not in user_ids
