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
    5. 種類（context_type）の違うセッションに混ぜて記録できないこと

外部サービスには繋がない:
    answer_question_stream（GeminiとQdrantを呼ぶ）を monkeypatch で差し替え、
    どんな引数で呼ばれたかを記録する。
    DBはテスト用のPostgreSQL（DATABASE_URL で指定）を使い、
    共通フィクスチャ temp_db がテストごとにテーブルを空にする。

検索フィルタそのもの（共通＋対象社員の個別だけを見る）は
tests/test_search_filter.py で別途固定している。
"""

from collections.abc import Iterator

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


class RecordedCall:
    """answer_question_stream がどう呼ばれたかを覚えておく入れ物。"""

    def __init__(self) -> None:
        self.question: str | None = None
        self.role: str | None = None
        self.target_user_id: str | None = None
        self.profile: str | None = None
        self.self_user_id: str | None = None


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordedCall:
    """RAG（Gemini + Qdrant）を偽物に差し替え、呼び出し引数を記録する。"""
    call = RecordedCall()

    def fake_answer_question_stream(
        question: str,
        role: str,
        target_user_id: str | None = None,
        profile: str | None = None,
        self_user_id: str | None = None,
    ) -> tuple[list[str], Iterator[str]]:
        call.question = question
        call.role = role
        call.target_user_id = target_user_id
        call.profile = profile
        call.self_user_id = self_user_id
        return ["10", "20"], iter(["こんにちは", "、奥村さんの評価です。"])

    monkeypatch.setattr(chat_router, "answer_question_stream", fake_answer_question_stream)
    return call


def _ceo_headers() -> dict[str, str]:
    """社長としてログイン済みの状態を表す認証ヘッダを作る。"""
    token = create_access_token(user_id=ADMIN_USER_ID, role="ceo")
    return {"Authorization": f"Bearer {token}"}


def _employee_headers() -> dict[str, str]:
    """社員としてログイン済みの状態を表す認証ヘッダを作る。"""
    token = create_access_token(user_id=STAFF_USER_ID, role="employee")
    return {"Authorization": f"Bearer {token}"}


def test_社員のトークンでは403(temp_db: None) -> None:
    """管理者ロールのみ利用可能（require_ceo が効いている）。"""
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
        headers=_ceo_headers(),
    )

    assert response.status_code == 200
    assert recorded.target_user_id == STAFF_USER_ID
    assert recorded.role == "ceo"
    # 前後の空白は取り除かれてから渡る
    assert recorded.question == "直近の評価は？"


def test_対象社員の基本情報がプロンプトに渡る(temp_db: None, recorded: RecordedCall) -> None:
    """社員データ画面に出ている基本情報を、AIも参照できることを固定する（Issue #72）。

    これが渡らないと「家族構成は？」と聞いても答えられない。
    画面には表示されているのにAIは知らない、という食い違いになる。
    """
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "家族構成は？", "target_user_id": STAFF_USER_ID},
        headers=_ceo_headers(),
    )

    assert response.status_code == 200
    assert recorded.profile is not None
    # users.csv の user_id=2（EMP001 奥村仁哉）の値が渡っている
    assert "氏名: 奥村仁哉" in recorded.profile
    assert "家族構成: 配偶者・子1" in recorded.profile


def test_基本情報にパスワードとロールが含まれない(temp_db: None, recorded: RecordedCall) -> None:
    """users.csv には平文パスワードがある。AIに渡すとそのまま回答文に出かねない。

    ホワイトリスト方式（app.users.PROFILE_FIELD_LABELS）が効いていることを、
    APIを通した状態でも確かめておく。
    """
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "この人について教えて", "target_user_id": STAFF_USER_ID},
        headers=_ceo_headers(),
    )

    assert response.status_code == 200
    assert recorded.profile is not None
    assert "password" not in recorded.profile
    assert "employee" not in recorded.profile


def test_通常チャットには基本情報を渡さない(temp_db: None, recorded: RecordedCall) -> None:
    """/api/chat/stream は対象社員が決まらないので、基本情報を渡してはいけない。

    ここで渡してしまうと、社員が通常チャットから他人の生年月日や家族構成を
    引き出せる経路になり、権限ルールの趣旨に反する。
    """
    response = TestClient(app).post(
        "/api/chat/stream",
        json={"question": "有給は何日？"},
        headers=_ceo_headers(),
    )

    assert response.status_code == 200
    assert recorded.question == "有給は何日？"
    assert recorded.profile is None


def test_SSEでsourcesとtokenとdoneが順に流れる(temp_db: None, recorded: RecordedCall) -> None:
    """通常チャット（/api/chat/stream）と同じ順番・同じ形式で送られる。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": STAFF_USER_ID},
        headers=_ceo_headers(),
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
        headers=_ceo_headers(),
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
        headers=_ceo_headers(),
    )

    assert response.status_code == 404
    # 検索・生成まで進んでいない
    assert recorded.target_user_id is None


def test_管理者自身のuser_idは404(temp_db: None, recorded: RecordedCall) -> None:
    """スタッフ一覧に出ない人は詳細も見られない扱い（GET /api/admin/users/{user_id} と揃える）。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": ADMIN_USER_ID},
        headers=_ceo_headers(),
    )

    assert response.status_code == 404
    assert recorded.target_user_id is None


def test_空の質問は400(temp_db: None, recorded: RecordedCall) -> None:
    """空白だけの質問で生成を走らせない（通常チャットと同じ扱い）。"""
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "   ", "target_user_id": STAFF_USER_ID},
        headers=_ceo_headers(),
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
        headers=_ceo_headers(),
    )

    assert response.status_code == 403


def test_通常チャットのセッションを指定すると400(temp_db: None, recorded: RecordedCall) -> None:
    """自分のセッションでも、種類が general なら社員別チャットの記録先にはできない。

    持ち主が同じでも、1つの会話に参照範囲の違う発言が混ざると、
    後から履歴を読み返したときに、どの発言がどの社員について聞いたものか区別できない。
    """
    # 社長自身の「通常チャット」のセッションを先に作っておく
    general_session_id = create_session(user_id=ADMIN_USER_ID, context_type="general")

    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={
            "question": "評価は？",
            "target_user_id": STAFF_USER_ID,
            "session_id": general_session_id,
        },
        headers=_ceo_headers(),
    )

    assert response.status_code == 400
    # 検索・生成まで進んでいない（弾いてから生成する順序になっている）
    assert recorded.target_user_id is None


def test_社員別チャットのセッションは続けられる(temp_db: None, recorded: RecordedCall) -> None:
    """種類が一致していれば、同じセッションの続きとして記録できる（正常系）。"""
    session_id = create_session(user_id=ADMIN_USER_ID, context_type="staff_inquiry")

    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={
            "question": "評価は？",
            "target_user_id": STAFF_USER_ID,
            "session_id": session_id,
        },
        headers=_ceo_headers(),
    )
    assert response.status_code == 200

    conn = database.get_connection()
    sessions = conn.execute("SELECT session_id FROM chat_sessions").fetchall()
    messages = conn.execute("SELECT session_id FROM chat_messages").fetchall()
    conn.close()

    # 新しいセッションは作られず、指定したセッションに追記されている
    assert len(sessions) == 1
    assert [m["session_id"] for m in messages] == [session_id, session_id]


def test_社員別チャットのセッションを通常チャットに指定すると400(
    temp_db: None, recorded: RecordedCall
) -> None:
    """逆向き（staff_inquiry のセッションを /api/chat/stream に渡す）も同じく拒否する。"""
    staff_session_id = create_session(user_id=ADMIN_USER_ID, context_type="staff_inquiry")

    response = TestClient(app).post(
        "/api/chat/stream",
        json={"question": "有給は何日？", "session_id": staff_session_id},
        headers=_ceo_headers(),
    )

    assert response.status_code == 400
    assert recorded.question is None


def test_社員チャットではJWTのuser_idがself_user_idに渡る(
    temp_db: None, recorded: RecordedCall
) -> None:
    """/api/chat/stream に、トークンの user_id が self_user_id として渡ることを固定する。

    なぜこのテストが必要か（重要）:
        self_user_id は「この人の個別ソースを検索してよい」という指定そのもの。
        ここにリクエスト由来の値（ボディやクエリの user_id）が渡る作りにすると、
        社員が他人の user_id を送るだけで、その人の個別ソース
        （評価・給与）を読めてしまう。
        JWTは署名付きで改ざんできないので、そこから取り出した値だけが信用できる。
        「JWT由来の値が渡っている」ことを、APIを通した状態で固定しておく。
    """
    response = TestClient(app).post(
        "/api/chat/stream",
        json={"question": "私の資格は？"},
        headers=_employee_headers(),
    )

    assert response.status_code == 200
    assert recorded.self_user_id == STAFF_USER_ID
    assert recorded.role == "employee"


def test_社員別チャットではself_user_idを渡さない(temp_db: None, recorded: RecordedCall) -> None:
    """/api/chat/staff-inquiry では self_user_id を渡さないことを固定する。

    なぜこのテストが必要か:
        この経路は require_ceo で守られており role='ceo' になる。
        search() の社員向けの分岐に入らないので、self_user_id を渡す意味がない。
        意味の無い値を渡さないでおけば、「誰の資料を見てよいか」を決める材料が
        target_user_id ひとつに保たれ、後から読んだときに経路を追いやすい。
    """
    response = TestClient(app).post(
        "/api/chat/staff-inquiry",
        json={"question": "評価は？", "target_user_id": STAFF_USER_ID},
        headers=_ceo_headers(),
    )

    assert response.status_code == 200
    assert recorded.self_user_id is None
    assert recorded.target_user_id == STAFF_USER_ID
