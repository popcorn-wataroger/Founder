"""チャット履歴のDB操作をまとめたモジュール。

書き込み系（create_session / add_message）と、
取得系（get_messages / get_sessions / get_session_owner）をまとめている。

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


def get_session_owner(session_id: int) -> str | None:
    """指定セッションの持ち主（user_id）を返す。存在しなければ None。

    入力:
        session_id … 調べたいセッションのID

    出力:
        そのセッションを作った人の user_id。該当セッションが無ければ None

    何のための関数か（重要）:
        「このセッションを見てよい人か」を判定するために使う。
        session_id は連番なので、社員が /api/chat/sessions/3/messages のように
        他人のIDを推測して叩くことは簡単にできてしまう。
        メッセージを返す前に、この関数で持ち主を確認し、
        本人（または社長）でなければ拒否する必要がある。
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT user_id FROM chat_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    conn.close()

    # 該当セッションが無ければ None を返す（呼び出し元で404にする）
    if row is None:
        return None

    return row["user_id"]


def get_messages(session_id: int) -> list[dict]:
    """指定セッションのメッセージ一覧を、古い順（時系列）で返す。

    入力:
        session_id … 取得したいセッションのID

    出力:
        メッセージの辞書のリスト。会話の流れ順（古い→新しい）に並ぶ
        例: [{"message_id": 1, "role": "user", "content": "...", "created_at": "..."} , ...]

    処理:
        chat_messages から、その session_id のメッセージを message_id 順に取り出す。
        message_id は自動採番の連番なので、この順＝記録された順＝会話の流れ順になる。

    権限について:
        この関数は権限チェックをしない（DB操作に徹する）。
        「誰が見てよいか」の判定は呼び出し元（ルーター）の責任とし、
        get_session_owner で持ち主を確認してから呼ぶこと。
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT message_id, role, content, created_at, referenced_sources
        FROM chat_messages
        WHERE session_id = ?
        ORDER BY message_id
        """,
        (session_id,),
    ).fetchall()
    conn.close()

    # sqlite3.Row のままだとJSONに変換できないので、辞書に直して返す
    return [dict(row) for row in rows]


def get_sessions(user_id: str) -> list[dict]:
    """指定ユーザーのセッション一覧を、新しい順で返す。

    入力:
        user_id … セッションを調べたい人の user_id

    出力:
        セッションの辞書のリスト（新しい順）
        例: [{"session_id": 3, "started_at": "...", "preview": "有給休暇は何日..."}, ...]
        preview … そのセッションで最初にユーザーが送った質問文（一覧表示用の見出し）

    処理:
        1. chat_sessions から、その user_id のセッションを新しい順に取り出す
        2. 各セッションについて、最初のユーザー発言を1件だけ取り出して preview にする

    なぜ preview を付けるか:
        一覧に session_id と日時だけ並べても、どの会話か分からない。
        「何を聞いた会話か」が一目で分かるよう、最初の質問文を見出しとして添える。
        社員データ画面の「最近のトーク」プレビュー表示にそのまま使える。

    権限について:
        この関数は user_id で絞り込むだけで、権限チェックはしない。
        「その user_id を指定してよい人か」の判定は呼び出し元（ルーター）の責任。
    """
    conn = get_connection()

    # 1. そのユーザーのセッションを新しい順（session_idの降順）に取り出す
    session_rows = conn.execute(
        """
        SELECT session_id, started_at, context_type
        FROM chat_sessions
        WHERE user_id = ?
        ORDER BY session_id DESC
        """,
        (user_id,),
    ).fetchall()

    sessions: list[dict] = []
    for row in session_rows:
        # 2. そのセッションで最初にユーザーが送った質問を1件だけ取り出す
        #    role='user' に絞るのは、AIの回答ではなく「何を聞いたか」を見出しにしたいため
        first_message = conn.execute(
            """
            SELECT content
            FROM chat_messages
            WHERE session_id = ? AND role = 'user'
            ORDER BY message_id
            LIMIT 1
            """,
            (row["session_id"],),
        ).fetchone()

        sessions.append(
            {
                "session_id": row["session_id"],
                "started_at": row["started_at"],
                "context_type": row["context_type"],
                # メッセージが1件も無いセッション（回答生成に失敗した場合など）では None になる
                "preview": first_message["content"] if first_message else None,
            }
        )

    conn.close()

    return sessions
