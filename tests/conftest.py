"""テスト全体で共有するフィクスチャを置くファイル。

pytest は conftest.py に書いたフィクスチャを、同じディレクトリ以下の
全テストファイルから自動で見つけてくれる（import は不要）。

なぜテストもPostgreSQLを使うか:
    本番が Cloud SQL(PostgreSQL) なので、テストだけSQLiteにすると
    プレースホルダ（%s と ?）や RETURNING の有無といった方言の違いを
    テストが素通ししてしまう。「テストは通るが本番で落ちる」を避けるため、
    テストも同じPostgreSQLに繋ぐ（SQLiteとの二重サポートはしない）。

接続先:
    ローカル … cloud-sql-proxy を起動し、Cloud SQL 上の founder_test を指す
    CI       … GitHub Actions の service コンテナで立てた postgres:16
    どちらも環境変数 DATABASE_URL で指定する（app/database.py が読む）。
"""

import os
from collections.abc import Iterator

import pytest

from app import database
from app.config import DATABASE_URL

# テストのたびに空にするテーブル。
# 外部キーで繋がっている（chat_messages → chat_sessions）ため、
# 6つまとめて1文で TRUNCATE する。
# user_passwords を含めるのは、あるテストのログインで保存されたハッシュが
# 次のテストに残ると、テストの結果が実行順に左右されるため
TEST_TABLES = (
    "sources",
    "chat_sessions",
    "chat_messages",
    "user_logins",
    "user_roles",
    "user_passwords",
)

_NO_DATABASE_URL_MESSAGE = (
    "DATABASE_URL が設定されていないため、DBを使うテストを実行できません。"
    "ローカルでは cloud-sql-proxy を起動し、テスト用の founder_test データベースを指す "
    "DATABASE_URL を設定してください。"
    "CIでは .github/workflows/ci.yml の test ジョブが PostgreSQL の service コンテナと "
    "DATABASE_URL を用意します。"
)

# テスト用データベースだと判断するための、データベース名に含まれるべき文字列。
# ローカルの founder_test も、CIの founder_test も、どちらもこれを含む。
_TEST_DB_NAME_MARKER = "test"


def _wrong_database_message(database_name: str) -> str:
    """接続先がテスト用でないときに出すエラーメッセージを組み立てる。

    入力: 実際に接続したデータベース名
    処理: 何が起きたかと、どう直すかを1つの文にまとめる
    出力: pytest.fail に渡す文字列
    """
    return (
        f"接続先のデータベース名が '{database_name}' です。"
        f"名前に '{_TEST_DB_NAME_MARKER}' を含まないため、"
        "本番用のデータベースを指している可能性があります。"
        "テーブルを空にせずに中断しました。"
        "DATABASE_URL の末尾をテスト用データベースに変更してから実行してください。"
        "例: postgresql://founder:<PASSWORD>@localhost:5432/founder_test"
    )


def _truncate_all_tables() -> None:
    """テスト用DBの6テーブルを空にし、IDの採番も1に戻す。

    入力: なし（接続先は DATABASE_URL）
    処理: 接続先がテスト用DBか確認してから TRUNCATE する
    出力: なし（副作用としてテーブルの中身が消える）
    例外: 接続先がテスト用DBでなければ pytest.fail で中断する

    なぜ接続先をコードで確認するか（重要）:
        この関数は接続先のテーブルを問答無用で空にする。
        DATABASE_URL の末尾が founder（開発用）のままテストを実行すると、
        ソース・チャット履歴・最終ログインが1件残らず消える。
        README にも注意書きを置いているが、文章は読み飛ばされるし、
        シェルに前回の export が残っていることにも気づきにくい。
        取り違えを本当に防げるのは、消す直前に自分で確かめて止まる仕組みだけなので、
        警告文ではなくコードで止める。

    なぜ RESTART IDENTITY を付けるか:
        TRUNCATE だけだと行は消えても連番の採番位置は進んだままになる。
        すると前のテストの影響で「1件目に登録したソースの source_id は 1」という
        前提のテストが、2回目以降の実行で壊れる。
        採番も一緒に戻すことで、どのテストも同じ状態から始められる。

    なぜ CASCADE を付けるか:
        chat_messages が chat_sessions を外部キーで参照している。
        参照されているテーブルを単体で TRUNCATE しようとすると
        PostgreSQL がエラーにするため、関連するテーブルもまとめて対象にする。
    """
    conn = database.get_connection()

    # 実際に繋がった先の名前をDBに聞く。URLの文字列を見るのではなく接続先に確認するのは、
    # URLの書き方（ソケット指定など）に関係なく同じ判定になるようにするため
    row = conn.execute("SELECT current_database()").fetchone()
    database_name = row["current_database"] if row else ""

    if _TEST_DB_NAME_MARKER not in database_name:
        # 1行も消さずに中断する。中断する前に接続は閉じておく
        conn.close()
        pytest.fail(_wrong_database_message(database_name))

    conn.execute(f"TRUNCATE {', '.join(TEST_TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


@pytest.fixture
def temp_db() -> Iterator[None]:
    """テスト1件ごとに、空のテーブルが用意されたDBを使えるようにする。

    入力: なし
    出力: なし（テスト本体はこのフィクスチャを受け取るだけでよい）

    処理:
        1. DATABASE_URL が設定されているか確かめる
        2. init_db() でテーブルを用意する（既にあれば何も起きない）
        3. 6テーブルを空にして、IDの採番も1に戻す
        4. テストへ処理を渡す

    なぜ function スコープ（テストごと）か:
        テストの実行順や実行するテストの組み合わせによって結果が変わらないようにするため。
        あるテストが入れたデータが次のテストに残っていると、
        単体では通るのに全部まとめて実行すると落ちる、という状態になる。

    DATABASE_URL が無いときの扱い:
        CI（GitHub Actions が CI=true を立てる）では失敗させる。
        ここで黙ってスキップすると、DBを使うテストが1件も実行されていないのに
        CIが緑になり、壊れたまま main にマージできてしまう。
        ローカルでは接続設定がまだの場合もあるためスキップに留める。
    """
    if not DATABASE_URL:
        if os.getenv("CI"):
            pytest.fail(_NO_DATABASE_URL_MESSAGE)
        pytest.skip(_NO_DATABASE_URL_MESSAGE)

    database.init_db()
    _truncate_all_tables()

    yield
