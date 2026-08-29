"""データベース（Cloud SQL / PostgreSQL）への接続・テーブル作成・初期投入を担当するモジュール。

接続先は環境変数 DATABASE_URL だけで決まる。
    ローカル: Cloud SQL Auth Proxy を起動し、proxy が待ち受ける localhost へ繋ぐURL
    Cloud Run: Unix ソケット（/cloudsql/接続名）を指定するURL

このモジュールにGCP固有の処理（プロジェクトIDやインスタンス接続名の組み立て、
認証情報の取得など）は持たせない。環境ごとの違いはURLの文字列の違いに閉じ込め、
コード側は「渡されたURLに繋ぐ」だけにする。そうしておけば、実行環境が増えても
このファイルを触らずに済み、テスト時に別のPostgreSQLへ向けることもできる。
"""

import csv
from contextlib import closing
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

# 社員マスタの初期データが入っているCSVのパス。
#
# なぜ users.py ではなくこのファイルに置くか:
#     app/users.py は社員マスタを読むために app/database.py の get_connection() を使う。
#     逆向きに database.py が users.py を import すると相互参照になり、
#     Python が読み込みの途中で止まってアプリが起動しなくなる。
#     初期投入はテーブル作成と同じ「DBを使える状態にする」作業なので、
#     依存の向きを一方向（users.py → database.py）に保ったまま
#     こちら側に置く。app/users.py からは同じ名前で再公開している。
USERS_CSV_PATH = Path("data/users.csv")

# users テーブルの列。data/users.csv のヘッダーと同じ名前・同じ並びにしてある。
#
# 定数として1箇所にまとめる理由:
#     CREATE TABLE と INSERT の両方でこの並びを使う。
#     2箇所に書き下すと、列を足したときに片方だけ直して
#     「テーブルにはあるが投入されない列」ができる。
USER_COLUMNS: tuple[str, ...] = (
    "user_id",
    "employee_code",
    "name",
    "department",
    "gender",
    "birth_date",
    "family",
    "hire_date",
    "employment_type",
    "password",
    "role",
    "last_login_at",
)

# user_id を採番するシーケンスの名前。
#
# なぜシーケンスで採番するか（重要）:
#     アカウント追加が2件同時に来ても、同じ user_id を2人に渡さないため。
#     nextval() は「今の値を1つ進めて返す」をDB側でアトミックに行い、
#     しかもトランザクションの外側で進む。そのため、同時に呼ばれても
#     同じ値が2回返ることがない。アプリ側でロックを書く必要もない。
#
#     「SELECT MAX(user_id) + 1」方式にしなかった理由:
#         同時に実行すると両方が同じ MAX を読み、片方が主キー違反で落ちる。
#         「同じ番号にならない」こと自体は主キー制約が守ってくれるが、
#         守り方が「片方を失敗させる」なので、リトライを自前で書くことになる。
#
#     user_id を IDENTITY 列にしなかった理由:
#         user_id は TEXT で、user_logins / sources.owner_user_id / JWT の中身が
#         すべて文字列として扱っている。型を変えると影響がこの段階の外まで広がる。
USER_ID_SEQUENCE = "users_user_id_seq"

# employee_code の一意制約の名前。
#
# 明示的に名前を付ける理由:
#     アカウント追加が一意制約違反で失敗したとき、
#     それが「社員コードの重複」なのか「user_id の重複」なのかを
#     app/users.py が名前で見分ける。名前を PostgreSQL の既定に任せると、
#     どの名前が付くかがこのコードから読み取れなくなる。
#     なお既定の名前も users_employee_code_key なので、
#     この宣言を足す前に作られたテーブルとも一致する。
USERS_EMPLOYEE_CODE_UNIQUE = "users_employee_code_key"


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
    """データベースの初期化（テーブル作成と、社員マスタの初期投入）。

    入力: なし（接続先は get_connection 経由で DATABASE_URL から決まる）
    処理: with closing(get_connection()) as conn: で接続し、7テーブル
          （users / sources / chat_sessions / chat_messages / user_logins /
          user_roles / user_passwords）を
          CREATE TABLE IF NOT EXISTS で作り、user_id 採番用のシーケンスも作って
          commit して確定する
          （commit は with ブロックの内側で行い、close は with を抜けるときに任せる）。
          そのあと seed_users_if_empty() を呼び、users が空なら
          data/users.csv の内容を投入して、採番の現在値を合わせる
    出力: なし（副作用としてテーブルが作られ、初回だけ社員マスタが入る）
    例外: DATABASE_URL が未設定・空のとき RuntimeError（get_connection が投げる）

    closing を使う理由:
        CREATE TABLE の途中で例外が出ても、with を抜けるときに必ず接続が閉じられるため。
        Cloud SQL は同時接続数に上限があり、閉じ忘れが溜まると新規接続が拒否される。

    IF NOT EXISTS を付けているため、既にテーブルがある状態で呼んでも何も起きない。
    アプリの起動時（app/main.py の lifespan）とテストの準備で毎回呼ばれる。

    なぜ初期投入をこの関数の中でやるか:
        init_db() を呼んだ側から見て「DBが使える状態になった」で揃えるため。
        テーブル作成と初期投入を別々の関数にして呼び出し側に2回呼ばせると、
        片方だけ呼ぶ経路（テストの準備など）ができたときに
        社員が1人も居ないDBが出来上がり、ログインが全部失敗する。
    """
    with closing(get_connection()) as conn:
        # 社員マスタ。列は data/users.csv のヘッダーと同じ12列。
        # employee_code に一意制約を付けるのは、ログインがこの値で社員を1人に決めるため。
        # 重複した行が入ると、どちらの社員としてログインしたのか決まらなくなる。
        #
        # 空欄になりうる列（部署・生年月日など）を NULL 可にせず DEFAULT '' にしている理由:
        #     CSVの空欄はこれまで空文字として扱われてきた（csv.DictReader の挙動）。
        #     NULL を入れると社員データAPIの応答が "" から null に変わり、
        #     画面の表示も変わってしまう。値の形をこれまでと同じに保つ。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id         TEXT PRIMARY KEY,
                employee_code   TEXT NOT NULL
                                CONSTRAINT users_employee_code_key UNIQUE,
                name            TEXT NOT NULL,
                department      TEXT NOT NULL DEFAULT '',
                gender          TEXT NOT NULL DEFAULT '',
                birth_date      TEXT NOT NULL DEFAULT '',
                family          TEXT NOT NULL DEFAULT '',
                hire_date       TEXT NOT NULL DEFAULT '',
                employment_type TEXT NOT NULL DEFAULT '',
                password        TEXT NOT NULL DEFAULT '',
                role            TEXT NOT NULL,
                last_login_at   TEXT NOT NULL DEFAULT ''
            )
        """)
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
        # 最終ログイン日時。ログインのたびに変わる値を users とは別に持つ（1社員1行）。
        # 元々は社員マスタがGit管理下のCSVで書き戻せなかったため分けた。
        # users がテーブルになった今も、まとめるかどうかは
        # アカウント管理の実装（Issue #123 の後続）で判断する
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_logins (
                user_id       TEXT PRIMARY KEY,
                last_login_at TEXT NOT NULL
            )
        """)
        # ロールの上書き。users.role は初期値で、運用中の変更はこちらに積む（1社員1行）。
        # 誰がいつ変えたかを残すため updated_at / updated_by も一緒に持つ
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_roles (
                user_id    TEXT PRIMARY KEY,
                role       TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            )
        """)
        # パスワード。user_logins / user_roles と同じく、
        # 運用中に変わる値は users とは別のテーブルに持つ（1社員1行）。
        # 持つのはハッシュ化した文字列だけで、平文のパスワードは保存しない
        # （users.password はCSV由来の初期値で、初回ログイン時にこちらへ移行する）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_passwords (
                user_id       TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
        """)
        # user_id の採番用。値の合わせ方は _sync_user_id_sequence() を参照。
        # IF NOT EXISTS なので、既にあるDBで起動しても現在値は動かない
        conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {USER_ID_SEQUENCE} AS BIGINT")
        conn.commit()

    # テーブルが揃ってから社員マスタを入れる（初回だけ）。
    # 接続を分けているのは、この関数をテストの準備からも単体で呼べるようにするため
    seed_users_if_empty()


def seed_users_if_empty() -> None:
    """users テーブルが空なら data/users.csv の内容を投入し、採番の現在値を合わせ直す。

    入力: なし（接続先は get_connection 経由で DATABASE_URL から決まる）

    処理:
        1. users の行数を数える
        2. 空なら data/users.csv を1行ずつ読み、users へ INSERT して commit する
           （1行でもあれば投入は飛ばす。既にあるデータを一切触らない）
        3. どちらの場合も _sync_user_id_sequence() で採番の現在値を合わせる

    出力:
        なし（副作用として、初回だけ社員マスタの行が入る）

    なぜ「空のときだけ」なのか（重要）:
        users は運用が始まれば書き換わるテーブルになる（ロール変更・アカウント追加）。
        起動のたびにCSVで上書きすると、画面から行った変更が
        次のデプロイや再起動で消える。CSVはあくまで「まだ1件も無いときの初期値」であり、
        運用中の正はDB側、という関係にする。
        user_roles / user_passwords が「変更はDBに積み、元の値は既定値として使う」
        としているのと同じ考え方。

    なぜ init_db() の内側の private 関数ではなく、公開関数にしたか:
        テストが users テーブルを空にしたあと（tests/conftest.py の temp_db）、
        社員マスタだけを入れ直すために単体で呼べる必要があるため。
        呼び出し側で接続を用意させると使う側の手数が増えるので、
        接続はこの関数の中で開いて閉じる。

    ON CONFLICT DO NOTHING を付けている理由:
        Cloud Run はインスタンスを複数同時に立ち上げることがある。
        2つのインスタンスが同時に「空だ」と判断すると、同じ行を2回入れようとして
        主キー違反で起動に失敗する。後から入れた側を黙って捨てれば、
        どちらの順番でも結果は同じ（CSVの内容が1回だけ入った状態）になる。

    空欄の扱い:
        csv.DictReader は空欄を空文字で返す。値が欠けている場合（列数が足りない行）は
        None が入るため、そのときも空文字に寄せる。
        NULL を入れないのは、社員データAPIの応答を今までと同じ形に保つため。
    """
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        existing_count = row["count"] if row else 0

        # 1行も無いときだけCSVを流し込む（運用中のデータは上書きしない）
        if existing_count == 0:
            with open(USERS_CSV_PATH, encoding="utf-8") as f:
                csv_rows = list(csv.DictReader(f))

            # 列名も値の並びも USER_COLUMNS ひとつから組み立てる（書き下しの食い違いを防ぐ）。
            # SQL文に埋め込むのはこのファイルが持つ定数の列名だけで、
            # CSV由来の値は必ず %s プレースホルダでバインドする（文字列連結しない）
            column_list = ", ".join(USER_COLUMNS)
            placeholders = ", ".join(["%s"] * len(USER_COLUMNS))
            insert_sql = (
                f"INSERT INTO users ({column_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            )

            for csv_row in csv_rows:
                values = tuple(csv_row.get(column) or "" for column in USER_COLUMNS)
                conn.execute(insert_sql, values)

            conn.commit()

        # 投入の有無にかかわらず、採番の現在値を今の中身に合わせる
        _sync_user_id_sequence(conn)
        conn.commit()


def _sync_user_id_sequence(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """user_id の採番シーケンスを「users の最大値の次」に合わせる。

    入力:
        conn … seed_users_if_empty() が開いている接続

    処理:
        users にある数字だけの user_id の最大値を調べ、
        次の nextval() がその +1 を返すようにシーケンスを設定する。
        1件も無ければ次の nextval() は 1 を返す。

    出力:
        なし（副作用としてシーケンスの現在値が変わる）

    なぜ必要か:
        シーケンスは CREATE した直後、1 から数え始める。
        既に user_id が 1〜9 の社員が居るDBでそのまま nextval() を使うと
        1 が返り、主キー違反でアカウント追加が必ず失敗する。
        起動のたびに現在の中身へ合わせておけば、その食い違いが起きない。

    setval の第3引数に false を渡している理由:
        false は「この値をまだ配っていない」という意味で、
        次の nextval() が渡した値そのものを返す。
        MAX が 9 なら 10 を渡して次が 10、1件も無ければ 1 を渡して次が 1 になる。
        true にすると「渡した値は配り済み」となり、その次から配られるため、
        件数が0のときに 0 を渡せずシーケンスの最小値(1)を割ってしまう。

    GREATEST で「進んだ値を下げない」ようにしていない理由:
        下げても衝突しないため。シーケンスだけが進んで users に行が無い状態は、
        採番したあとの INSERT が失敗して取り消されたときにできる欠番であり、
        その番号を持つ社員は存在しない。次に配り直しても重複にならない。
        巻き戻さない作りにすると、テストのたびに採番が進んで
        「追加したアカウントの user_id」が実行順で変わり、テストが読みにくくなる。

    数字だけを対象にしている理由:
        user_id は TEXT なので、将来 UUID のような値が混ざると
        整数への変換(::int)が失敗して起動できなくなる。
        正規表現で数字だけの行に絞り、変換できる値だけを見る。
    """
    conn.execute(
        f"""
        SELECT setval(
            '{USER_ID_SEQUENCE}',
            COALESCE(
                (SELECT MAX(user_id::bigint) FROM users WHERE user_id ~ '^[0-9]+$'),
                0
            ) + 1,
            false
        )
        """
    )
