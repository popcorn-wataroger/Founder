"""チャット履歴のDB操作をまとめたモジュール。

書き込み系（create_session / add_message）と、
取得系（get_messages / get_sessions / get_session / get_session_owner）をまとめている。

DB操作の流儀:
    with closing(get_connection()) as conn: で接続 → execute（%sプレースホルダ）→ commit
    （commit は with ブロックの内側で行い、close は with を抜けるときに任せる）

    closing を使う理由:
        execute の途中で例外が出ても、with を抜けるときに必ず接続が閉じられるため。
        Cloud SQL は同時接続数に上限があり、閉じ忘れが溜まると新規接続が拒否される。
"""

from contextlib import closing
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
        3. RETURNING で採番された session_id を受け取り、commit して確定して返す
    """
    # 開始時刻を文字列で用意（DBのカラムはTEXT型なのでISO形式の文字列で持つ）
    started_at = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as conn:
        # RETURNING で採番された session_id を受け取る（この後メッセージ記録で使う）。
        # 値は commit の前に fetchone() で取り出す
        cursor = conn.execute(
            """
            INSERT INTO chat_sessions (user_id, started_at, context_type)
            VALUES (%s, %s, %s)
            RETURNING session_id
            """,
            (user_id, started_at, context_type),
        )
        row = cursor.fetchone()
        conn.commit()
        session_id = row["session_id"] if row else None

    # INSERT が成功していれば RETURNING は必ず1行返す。
    # None なら採番できていない＝本来ありえない状態なので、ここで止める
    if session_id is None:
        raise RuntimeError(
            f"chat_sessions への INSERT で session_id が採番されませんでした user_id={user_id}"
        )

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
        3. RETURNING で採番された message_id を受け取り、commit して確定して返す

    補足:
        referenced_sources（参照ソースID）はこの段階では扱わないため NULL のまま。
    """
    # 記録時刻を文字列で用意
    created_at = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as conn:
        # RETURNING で採番された message_id を受け取る。値は commit の前に fetchone() で取り出す
        cursor = conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, created_at)
            VALUES (%s, %s, %s, %s)
            RETURNING message_id
            """,
            (session_id, role, content, created_at),
        )
        row = cursor.fetchone()
        conn.commit()
        message_id = row["message_id"] if row else None

    # INSERT が成功していれば RETURNING は必ず1行返す。
    # None なら採番できていない＝本来ありえない状態なので、ここで止める
    if message_id is None:
        raise RuntimeError(
            "chat_messages への INSERT で message_id が採番されませんでした "
            f"session_id={session_id}"
        )

    return message_id


def get_session(session_id: int) -> dict | None:
    """指定セッションの持ち主と会話の種類を返す。存在しなければ None。

    入力:
        session_id … 調べたいセッションのID

    出力:
        {"user_id": 持ち主, "context_type": 会話の種類} の辞書。
        該当セッションが無ければ None

    何のための関数か:
        「そのセッションに書き込んでよいか」の判定には、持ち主だけでなく
        会話の種類（general / staff_inquiry）も必要になる。
        通常チャットのセッションに社員別チャットの発言を混ぜられると、
        1つの会話の中で参照範囲の違う発言が並んでしまうため、
        呼び出し元（_resolve_session）が両方を突き合わせて判定する。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT user_id, context_type FROM chat_sessions WHERE session_id = %s",
            (session_id,),
        ).fetchone()

    # 該当セッションが無ければ None を返す（呼び出し元で404にする）
    if row is None:
        return None

    return dict(row)


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

    get_session との使い分け:
        持ち主だけ分かればよい呼び出し元のための薄い入口。
        SQLを二重に持たないよう、中身は get_session に任せている。
    """
    session = get_session(session_id)

    # 該当セッションが無ければ None を返す（呼び出し元で404にする）
    if session is None:
        return None

    owner_user_id: str = session["user_id"]
    return owner_user_id


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
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT message_id, role, content, created_at, referenced_sources
            FROM chat_messages
            WHERE session_id = %s
            ORDER BY message_id
            """,
            (session_id,),
        ).fetchall()

    # get_connection() が dict_row を使っているため各行はすでに辞書だが、
    # 「この関数はJSONにできる辞書を返す」ことを呼び出し元に約束するため dict() を通す
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
    sessions: list[dict] = []

    with closing(get_connection()) as conn:
        # 1. そのユーザーのセッションを新しい順（session_idの降順）に取り出す
        session_rows = conn.execute(
            """
            SELECT session_id, started_at, context_type
            FROM chat_sessions
            WHERE user_id = %s
            ORDER BY session_id DESC
            """,
            (user_id,),
        ).fetchall()

        for row in session_rows:
            # 2. そのセッションで最初にユーザーが送った質問を1件だけ取り出す
            #    role='user' に絞るのは、AIの回答ではなく「何を聞いたか」を見出しにしたいため
            first_message = conn.execute(
                """
                SELECT content
                FROM chat_messages
                WHERE session_id = %s AND role = 'user'
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

    return sessions
