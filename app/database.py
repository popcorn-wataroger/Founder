"""データベース（Cloud SQL / PostgreSQL）への接続とテーブル作成を担当するモジュール。

接続先は環境変数 DATABASE_URL だけで決まる。
    ローカル: Cloud SQL Auth Proxy を起動し、proxy が待ち受ける localhost へ繋ぐURL
    Cloud Run: Unix ソケット（/cloudsql/接続名）を指定するURL

このモジュールにGCP固有の処理（プロジェクトIDやインスタンス接続名の組み立て、
認証情報の取得など）は持たせない。環境ごとの違いはURLの文字列の違いに閉じ込め、
コード側は「渡されたURLに繋ぐ」だけにする。そうしておけば、実行環境が増えても
このファイルを触らずに済み、テスト時に別のPostgreSQLへ向けることもできる。
"""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    """PostgreSQLへの接続を返す。

    入力: なし（環境変数由来の config.DATABASE_URL を読む）
    出力: psycopg の Connection（行を dict で返す設定にしたもの）
    例外: DATABASE_URL が未設定・空のとき RuntimeError

    row_factory に dict_row を指定している理由:
        呼び出し側は取り出した行を dict(row) や row["列名"] の形で扱っている。
        psycopg の既定はタプルを返すため、そのままだと列名でのアクセスができない。
        dict_row にすると行が辞書で返り、これまでの書き方をそのまま使える。

    型注釈に [dict[str, Any]] を付けている理由:
        psycopg.Connection は「1行をどの型で返すか」を型引数に持つ。
        省略すると既定のタプル版とみなされ、dict_row を渡している実装と
        食い違って mypy がエラーにする。
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL が設定されていません。"
            "ローカルで動かす場合は cloud-sql-proxy を起動してから、"
            "proxy の待ち受け先を指す DATABASE_URL を設定してください。"
        )
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db() -> None:
    """データベースの初期化（テーブル作成）。

    入力: なし（接続先は get_connection 経由で DATABASE_URL から決まる）
    処理: 4テーブル（sources / chat_sessions / chat_messages / user_logins）を
          CREATE TABLE IF NOT EXISTS で作り、commit して確定する
    出力: なし（副作用としてテーブルが作られる）
    例外: DATABASE_URL が未設定・空のとき RuntimeError（get_connection が投げる）

    IF NOT EXISTS を付けているため、既にテーブルがある状態で呼んでも何も起きない。
    アプリの起動時（app/main.py の lifespan）とテストの準備で毎回呼ばれる。
    """
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            source_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
            session_id   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id      TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            context_type TEXT NOT NULL DEFAULT 'general'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            message_id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
