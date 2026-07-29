"""社員別チャットAPI（POST /api/chat/staff-inquiry）のテスト。

対象:
    POST /api/chat/staff-inquiry … 社長が「この社員について」質問するAPI（SSE）

何を守りたいか（Issue #24 の完了条件）:
    1. 管理者ロールのみ利用できること（社員のトークンでは403）
    2. 対象社員（target_user_id）が検索まで渡っていること
       ＝ 他の社員の個別ソースが参照されない仕組みが働いていること
    3. セッションの持ち主が「質問した社長」であり、
       context_type が 'staff_inquiry' で記録されること
    4. 存在しない社員・管理者自身を指定した場合は404になること

外部サービスには繋がない:
    answer_question_stream（GeminiとQdrantを呼ぶ）を monkeypatch で差し替え、
    どんな引数で呼ばれたかを記録する。
    DBも tmp_path の使い捨てファイルに差し替えて、本番の data/founder.db を汚さない。

検索フィルタそのもの（共通＋対象社員の個別だけを見る）は
tests/test_search_filter.py で別途固定している。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database
from app.chat_history import create_session
from app.main import app
from app.routers import chat_router
from app.routers.auth_router import create_access_token

# users.csv 上の想定: user_id=1 は管理者(ADMIN)、user_id=2 は社員(EMP001)
ADMIN_USER_ID = "1"
STAFF_USER_ID = "2"


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """本番の data/founder.db を汚さないよう、使い捨てのDBに差し替える。"""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    yield


class RecordedCall:
    """answer_question_stream がどう呼ばれたかを覚えておく入れ物。"""

    def __init__(self) -> None:
        self.question: str | None = None
        self.role: str | None = None
        self.target_user_id: str | None = None


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordedCall:
    """RAG（Gemini + Qdrant）を偽物に差し替え、呼び出し引数を記録する。"""
    call = RecordedCall()

    def fake_answer_question_stream(
        question: str,
        role: str,
        target_user_id: str | None = None,
    ) -> tuple[list[str], Iterator[str]]:
        call.question = question
        call.role = role
        call.target_user_id = target_user_id
        return ["10", "20"], iter(["こんにちは", "、奥村さんの評価です。"])

    monkeypatch.setattr(chat_router, "answer_question_stream", fake_answer_question_stream)
    return call


def _admin_headers() -> dict[str, str]:
    """社長としてログイン済みの状態を表す認証ヘッダを作る。"""
    token = create_access_token(user_id=ADMIN_USER_ID, role="admin")
    return {"Authorization": f"Bearer {token}"}


def test_社員のトークンでは403(temp_db: None) -> None:
    """管理者ロールのみ利用可能（require_admin が効いている）。"""
    token = create_access_token(user_id=STAFF_USER_ID, role="employee")
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": "3"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_未ログインでは401(temp_db: None) -> None:
    """トークンが無い場合は認証エラーになる。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": STAFF_USER_ID},
    )

    assert response.status_code == 401


def test_対象社員が検索まで渡る(temp_db: None, recorded: RecordedCall) -> None:
    """target_user_id がそのまま RAG に渡ることを固定する。

    ここが渡らないと社長の通常検索と同じ＝全社員の個別ソースが対象になり、
    「他の社員の個別ソースは絶対に参照しない」という完了条件が崩れる。
    """
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "  直近の評価は？  ", "target_user_id": STAFF_USER_ID},
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert recorded.target_user_id == STAFF_USER_ID
    assert recorded.role == "admin"
    # 前後の空白は取り除かれてから渡る
    assert recorded.question == "直近の評価は？"


def test_SSEでsourcesとtokenとdoneが順に流れる(temp_db: None, recorded: RecordedCall) -> None:
    """通常チャット（/api/chat/stream）と同じ順番・同じ形式で送られる。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": STAFF_USER_ID},
        headers=_admin_headers(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    # イベント名が想定どおり順に並ぶ（フロントはこの順番を前提に描画する）
    event_names = [
        line[len("event: ") :] for line in body.split("\n") if line.startswith("event: ")
    ]
    assert event_names[0] == "sources"
    assert event_names[-1] == "done"
    assert "token" in event_names

    # 参照ソースIDと本文の断片が、そのまま流れている
    assert '"referenced_sources": ["10", "20"]' in body
    assert "こんにちは" in body


def test_セッションは社長のものとして記録される(temp_db: None, recorded: RecordedCall) -> None:
    """持ち主は質問した社長、context_type は staff_inquiry になる。

    対象社員を持ち主にしてしまうと、社長の質問が社員本人の履歴として残り、
    社員データ画面の「最近のトーク」に本人が話していない会話が並んでしまう。
    """
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": STAFF_USER_ID},
        headers=_admin_headers(),
    )
    assert response.status_code == 200

    conn = database.get_connection()
    sessions = conn.execute("SELECT user_id, context_type FROM chat_sessions").fetchall()
    messages = conn.execute(
        "SELECT role, content FROM chat_messages ORDER BY message_id"
    ).fetchall()
    conn.close()

    assert len(sessions) == 1
    assert sessions[0]["user_id"] == ADMIN_USER_ID
    assert sessions[0]["context_type"] == "staff_inquiry"

    # 完走したので、質問と回答が履歴に残る（回答は断片をつないだ全文）
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "評価は？"),
        ("assistant", "こんにちは、奥村さんの評価です。"),
    ]


def test_存在しない社員は404(temp_db: None, recorded: RecordedCall) -> None:
    """user_id の指定間違いに社長が気づけるよう、黙って共通ソースで答えない。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": "99999"},
        headers=_admin_headers(),
    )

    assert response.status_code == 404
    # 検索・生成まで進んでいない
    assert recorded.target_user_id is None


def test_管理者自身のuser_idは404(temp_db: None, recorded: RecordedCall) -> None:
    """スタッフ一覧に出ない人は詳細も見られない扱い（GET /api/admin/users/{user_id} と揃える）。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": ADMIN_USER_ID},
        headers=_admin_headers(),
    )

    assert response.status_code == 404
    assert recorded.target_user_id is None


def test_空の質問は400(temp_db: None, recorded: RecordedCall) -> None:
    """空白だけの質問で生成を走らせない（通常チャットと同じ扱い）。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "   ", "target_user_id": STAFF_USER_ID},
        headers=_admin_headers(),
    )

    assert response.status_code == 400
    assert recorded.target_user_id is None


def test_他人のセッションには書き込めない(temp_db: None, recorded: RecordedCall) -> None:
    """session_id を指定する場合、自分のセッションでなければ403（通常チャットと同じ）。"""
    # 社員(user_id=2)名義のセッションを先に作っておく
    other_session_id = create_session(user_id=STAFF_USER_ID)

    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={
            "question": "評価は？",
            "target_user_id": STAFF_USER_ID,
            "session_id": other_session_id,
        },
        headers=_admin_headers(),
    )

    assert response.status_code == 403
