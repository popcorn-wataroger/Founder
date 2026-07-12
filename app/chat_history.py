"""チャット履歴のDB操作をまとめたモジュール。

この段階では「セッション作成」と「メッセージ記録」の書き込み系だけを実装している。
履歴の取得（SELECT系）は後続ステップで追加する。

DB操作の流儀は既存コード（sources_router.py 等）に合わせている:
    get_connection() で接続 → execute（?プレースホルダ）→ commit → close
"""

from datetime import datetime, timezone

from app.database import get_connection


def create_session(user_id: str, context_type: str = "general") -> int:
    """新しいチャットセッションを1件作成し、その session_id を返す。

    入力:
        user_id      … セッションの持ち主（誰の会話か）。usersテーブルのIDと対応
        context_type … 会話の種類（既定 'general'。社員データ画面からの問い合わせ等で使い分ける）

    出力:
        作成したセッションの session_id（自動採番された整数）

    処理:
        1. 現在時刻（UTC・ISO形式の文字列）を started_at として用意する
        2. chat_sessions に user_id, started_at, context_type を INSERT する
        3. commit して確定し、自動採番された session_id を lastrowid から取得して返す
    """
    # 開始時刻を文字列で用意（DBのカラムはTEXT型なのでISO形式の文字列で持つ）
    started_at = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO chat_sessions (user_id, started_at, context_type)
        VALUES (?, ?, ?)
        """,
        (user_id, started_at, context_type),
    )
    conn.commit()
    # 自動採番された session_id を取得（この後メッセージ記録で使う）
    session_id = cursor.lastrowid
    conn.close()

    return session_id


def add_message(session_id: int, role: str, content: str) -> int:
    """チャットメッセージを1件、chat_messages に記録し、その message_id を返す。

    入力:
        session_id … どのセッションのメッセージか（create_session の戻り値）
        role       … 発言者。'user'（社員の質問）か 'assistant'（AIの回答）
        content    … メッセージ本文（質問文または回答文）

    出力:
        記録したメッセージの message_id（自動採番された整数）

    処理:
        1. 現在時刻（UTC・ISO形式の文字列）を created_at として用意する
        2. chat_messages に session_id, role, content, created_at を INSERT する
        3. commit して確定し、自動採番された message_id を返す

    補足:
        referenced_sources（参照ソースID）はこの段階では扱わないため NULL のまま。
    """
    # 記録時刻を文字列で用意
    created_at = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO chat_messages (session_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, role, content, created_at),
    )
    conn.commit()
    message_id = cursor.lastrowid
    conn.close()

    return message_id
