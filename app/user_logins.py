"""最終ログイン日時（user_logins テーブル）のDB操作をまとめたモジュール。

書き込み系（record_login）と取得系（get_last_login_at）の2つだけを持つ。

なぜ data/users.csv ではなくDBに持つか:
    users.csv はGit管理下にあるため、ログインのたびに書き戻すと差分が出て
    `git status` が汚れる。CSV全体を書き換える方式は、失敗したときに
    他の社員の行まで壊すリスクもある。
    社員マスタ（滅多に変わらない）はCSVのまま読み取り専用にしておき、
    ログインのたびに変わる値だけをこのテーブルに分けて持つ。

DB操作の流儀は既存コード（chat_history.py 等）に合わせている:
    get_connection() で接続 → execute（%sプレースホルダ）→ commit → close

日時の形式:
    chat_sessions.started_at / sources.uploaded_at と同じく、
    UTC・ISO形式の文字列（例: 2026-08-01T10:23:45.123456+00:00）で保存する。
    画面側（static/js/admin.js）は new Date() でブラウザのローカル時刻に
    変換して表示するため、オフセット（+00:00）を必ず含める必要がある。
"""

from datetime import datetime, timezone

from app.database import get_connection


def record_login(user_id: str) -> str:
    """指定ユーザーの最終ログイン日時を「今」で記録し、記録した日時を返す。

    入力:
        user_id … ログインした社員の user_id（usersテーブル／users.csv のID）

    出力:
        記録した日時の文字列（UTC・ISO形式）

    処理:
        1. 現在時刻（UTC・ISO形式の文字列）を用意する
        2. user_logins に INSERT する。すでにその user_id の行があれば
           last_login_at だけを上書きする（UPSERT）
        3. commit して確定する

    なぜ UPSERT（ON CONFLICT DO UPDATE）か（重要）:
        user_id を主キーにして「1社員1行」を保つため。
        「消してから入れ直す」方式や、CSV全体を書き換える方式と違って、
        更新対象はその user_id の1行だけになるので、
        他の社員の記録が書き換わることが構造的に起こらない。

    セキュリティ:
        user_id はログインしてきた側から渡ってくる値なので、
        必ず %s プレースホルダでバインドする（SQL文に文字列連結しない）。
        こうしておけば、値に何が入っていてもSQLとして解釈されない。
    """
    # 記録時刻を文字列で用意（DBのカラムはTEXT型なのでISO形式の文字列で持つ）
    last_login_at = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    conn.execute(
        """
        INSERT INTO user_logins (user_id, last_login_at)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET last_login_at = excluded.last_login_at
        """,
        (user_id, last_login_at),
    )
    conn.commit()
    conn.close()

    return last_login_at


def get_last_login_at(user_id: str) -> str | None:
    """指定ユーザーの最終ログイン日時を返す。まだ一度もログインしていなければ None。

    入力:
        user_id … 調べたい社員の user_id

    出力:
        最終ログイン日時の文字列（UTC・ISO形式）。記録が無ければ None

    使いどころ:
        社員データ画面（GET /api/admin/users/{user_id}）の「最終ログイン」欄。
        None のときに画面へ何を出すかは呼び出し元の責任
        （現状は空文字を返し、画面側が「未記録」と表示する）。
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT last_login_at FROM user_logins WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    conn.close()

    # 一度もログインしていない社員は行そのものが無い
    if row is None:
        return None

    last_login_at: str = row["last_login_at"]
    return last_login_at
