import sqlite3
from pathlib import Path

# データベースファイルの保存場所
DB_PATH = Path("data/founder.db")


def get_connection() -> sqlite3.Connection:
    """SQLiteへの接続を返す"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # SQLiteは既定で外部キー制約を強制しないため、接続ごとに明示的に有効化する
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """データベースの初期化（テーブル作成）"""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            source_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name    TEXT NOT NULL,
            file_type    TEXT NOT NULL,
            file_path    TEXT NOT NULL,
            scope        TEXT NOT NULL DEFAULT 'common',
            owner_user_id TEXT,
            uploaded_at  TEXT NOT NULL,
            uploaded_by  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            context_type TEXT NOT NULL DEFAULT 'general'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id         INTEGER NOT NULL,
            role               TEXT NOT NULL,
            content            TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            referenced_sources TEXT,
            FOREIGN KEY (session_id) REFERENCES chat_sessions (session_id)
        )
    """)
    # 最終ログイン日時。社員マスタ（data/users.csv）はGit管理下で読み取り専用のため、
    # ログインのたびに変わる値だけをこちらに持つ（1社員1行）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_logins (
            user_id       TEXT PRIMARY KEY,
            last_login_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
