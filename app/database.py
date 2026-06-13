import sqlite3
from pathlib import Path

# データベースファイルの保存場所
DB_PATH = Path("data/founder.db")


def get_connection() -> sqlite3.Connection:
    """SQLiteへの接続を返す"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    conn.commit()
    conn.close()
