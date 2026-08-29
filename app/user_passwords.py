"""パスワード（user_passwords テーブル）のDB操作とハッシュ化をまとめたモジュール。

ハッシュ化系（hash_password / verify_password）と
DB読み書き系（get_password_hash / set_password）の4つだけを持つ。

なぜパスワードを平文で持たないか:
    data/users.csv には検証用の平文パスワードを置いていたが、
    CSVはGit管理下にあるため、リポジトリを読める人が全員のパスワードを読めてしまう。
    ハッシュにしておけば、保存値が漏れても元のパスワードは復元できない。

ソルトを別カラムで管理しない理由:
    bcrypt は生成したソルトをハッシュ文字列の中に埋め込む
    （$2b$12$<ソルト22文字><ハッシュ31文字> という形）。
    そのため保存するのはこの1本の文字列だけでよく、
    照合時も password_hash から自動でソルトが読み取られる。
    同じパスワードでも gensalt() のたびにソルトが変わるので、
    保存値は毎回異なる。「同じ保存値＝同じパスワード」という推測ができない。

このファイルに権限判定を書かない理由:
    ここはハッシュ化とDBの読み書きだけを担当する。
    「誰がパスワードを変えてよいか」の判定は app/routers/auth_router.py 側に置く。
    user_roles.py と同じ考え方で、判定を2箇所に書くと食い違うため役割を分ける。

DB操作の流儀:
    with closing(get_connection()) as conn: で接続 → execute（%sプレースホルダ）→ commit
    （commit は with ブロックの内側で行い、close は with を抜けるときに任せる）

    closing を使う理由:
        execute の途中で例外が出ても、with を抜けるときに必ず接続が閉じられるため。
        Cloud SQL は同時接続数に上限があり、閉じ忘れが溜まると新規接続が拒否される。

日時の形式:
    user_roles.updated_at / user_logins.last_login_at と同じく、
    UTC・ISO形式の文字列（例: 2026-08-01T10:23:45.123456+00:00）で保存する。
    画面側は new Date() でブラウザのローカル時刻に変換して表示するため、
    オフセット（+00:00）を必ず含める必要がある。

セキュリティ上の約束:
    パスワードの値そのもの（引数 password）は、ログにも例外メッセージにも出さない。
    ログは運用中に第三者の目に触れる場所であり、
    そこに平文が出た時点でハッシュ化した意味が消えるため。
    出してよいのは user_id や「照合に失敗した」という事実までにとどめる。
"""

from contextlib import closing
from datetime import datetime, timezone

import bcrypt

from app.database import get_connection

# bcrypt が扱えるパスワードの最大バイト数。
# ライブラリ側の仕様で、これを超える入力は bcrypt 5.0 以降 ValueError になる
# （古いバージョンのように黙って切り捨てられることはない）。
MAX_PASSWORD_BYTES = 72

# アカウント追加のときに要求するパスワードの最短の長さ（文字数）。
#
# ここに置く理由:
#     パスワードについての決まりごとを MAX_PASSWORD_BYTES と同じ場所にまとめる。
#     使う側（app/main.py のアカウント追加API）に 8 と直接書くと、
#     あとでログインAPIや変更APIにも同じ規則を足すときに数字が散って、
#     片方だけ直す事故が起きる。
#
# バイト数ではなく文字数で数える理由:
#     短さの制限は「推測されにくいか」の話なので、
#     日本語1文字を3バイトと数えて甘くしたくない。
#     長さの上限(MAX_PASSWORD_BYTES)だけは bcrypt の仕様に合わせてバイトで数える。
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    """パスワードをハッシュ化した文字列を返す。

    入力:
        password … 平文のパスワード

    処理:
        1. UTF-8 でのバイト数が MAX_PASSWORD_BYTES 以内かを確かめる
        2. gensalt() でソルトを新しく作る
        3. bcrypt.hashpw() でソルト込みのハッシュを作る（bytes で返る）
        4. DBにTEXTとして入れるため decode("utf-8") で文字列にする

    出力:
        ハッシュ文字列（例: $2b$12$... の形。ソルトを内側に含む）

    例外:
        password が MAX_PASSWORD_BYTES を超えるとき ValueError

    なぜ長さの上限があるか:
        bcrypt の仕様で、扱える入力は72バイトまで。
        bcrypt 5.0 はこれを超える入力を ValueError で拒否する。

    なぜ切り捨てずにエラーにするか:
        切り捨てると、73バイト目以降だけが違う2つのパスワードが
        同じものとして扱われる。
        つまり「入力した通りのパスワードで認証されていない」状態を、
        利用者にも運用側にも知らせないまま作ってしまう。
        またマルチバイト文字の途中でバイト列を切ると不正なUTF-8になり、
        別のところで文字化けや例外の原因になる。
        入らないものは入らないと言って止めるほうが安全側に倒れる。

    文字数ではなくバイト数で数える理由:
        bcrypt が見ているのはバイト列であって文字数ではない。
        UTF-8 では日本語1文字が3バイトになるため、
        文字数で数えると「英数字なら通るのに日本語だと落ちる」という
        条件によって変わる上限になってしまう。
        バイト数で数えれば、判定基準がライブラリの制限と1対1で対応する
        （日本語だけのパスワードなら24文字が上限になる）。

    例外メッセージにパスワードを含めない理由:
        例外メッセージはログにもエラー画面にも流れうる。
        平文が出た時点でハッシュ化した意味が消えるため、
        書くのはバイト数と上限だけにとどめる。

    毎回結果が変わる:
        gensalt() が呼び出しのたびに違うソルトを作るため、
        同じパスワードを渡しても戻り値は毎回異なる。
        したがって「== でハッシュ同士を比べる」照合はできない。
        照合は必ず verify_password() を使う。

    encode / decode をここで行う理由:
        bcrypt は bytes を受け取り bytes を返すライブラリだが、
        呼び出し元とDBが扱うのは str なので、
        bytes と str の変換をこの関数の中に閉じ込めておく。
        呼び出し元が bytes を意識しなくて済む。
    """
    # bcrypt に渡す前に長さを確かめる。
    # メッセージに出してよいのはバイト数と上限だけで、パスワードの値そのものは出さない
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"パスワードが長すぎます（{len(password_bytes)}バイト）。"
            f"UTF-8 で {MAX_PASSWORD_BYTES} バイトまでにしてください。"
        )

    # gensalt() は呼ぶたびに新しいソルトを作る。ソルトはハッシュ文字列の中に埋め込まれる
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """入力されたパスワードが保存されたハッシュと一致するかを返す。

    入力:
        password      … 画面から入力された平文のパスワード
        password_hash … DBに保存されているハッシュ文字列

    処理:
        bcrypt.checkpw() に両方を bytes で渡す。
        checkpw は password_hash に埋め込まれたソルトを取り出し、
        同じソルトで password をハッシュ化して突き合わせる。

    出力:
        一致すれば True、しなければ False

    例外を握りつぶして False を返す理由:
        password_hash が bcrypt の形式でない値（空文字、平文のまま残った値、
        壊れた文字列など）だと checkpw は ValueError を投げる。
        これをそのまま外に出すと、DBに1件でも不正な値があるだけで
        ログイン処理全体が500エラーで落ちる。
        「照合できなかった＝ログインさせない」と扱うほうが安全側に倒れるため、
        ここで捕まえて False を返す。
        通す方向（True）に倒す扱いは絶対にしない。

    セキュリティ:
        例外を捕まえたときも password の値はどこにも出さない。
        どの user_id で失敗したかを残したい場合は、呼び出し元でログを書く。
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # ハッシュの形式が不正。照合失敗として扱い、ログイン処理は続行させる
        return False


def get_password_hash(user_id: str) -> str | None:
    """指定ユーザーのパスワードハッシュを返す。記録が無ければ None。

    入力:
        user_id … 調べたい社員の user_id（users.csv のID）

    処理:
        user_passwords を user_id で1行引く。

    出力:
        ハッシュ文字列。行が無ければ None

    None の意味:
        「パスワードが空」ではなく「まだ設定されていない」。
        呼び出し元が None のときどうするか（ログインを断る、初期値を使う等）は
        この関数では決めない。get_role() と同じ組み立て方にしている。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT password_hash FROM user_passwords WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    # 一度もパスワードを設定していない社員は行そのものが無い
    if row is None:
        return None

    password_hash: str = row["password_hash"]
    return password_hash


def set_password(user_id: str, password: str) -> None:
    """指定ユーザーのパスワードを記録する。すでに記録があれば上書きする。

    入力:
        user_id  … パスワードを設定する社員の user_id
        password … 平文のパスワード（保存されるのはハッシュだけ）

    処理:
        1. hash_password() でハッシュ化する
        2. 現在時刻（UTC・ISO形式の文字列）を用意する
        3. user_passwords に INSERT する。すでにその user_id の行があれば
           password_hash / updated_at を上書きする（UPSERT）
        4. commit して確定する

    出力:
        なし（副作用としてテーブルの行が増える・書き換わる）

    平文はDBに渡らない:
        SQLに渡すのは hash_password() の戻り値だけ。
        引数の password はこの関数の中だけで使い、外にも保存先にも出ない。

    なぜ UPSERT（ON CONFLICT DO UPDATE）か:
        user_id を主キーにして「1社員1行」を保つため。
        「消してから入れ直す」方式と違って、更新対象はその user_id の1行だけになるので、
        他の社員のパスワードが書き換わることが構造的に起こらない。

    パスワードの強度をここで検証しない理由:
        文字数や使える文字の条件は呼び出し元（APIの入口）が決める。
        この関数は渡された値をそのままハッシュ化して保存する。
        検証を両方に置くと、条件が変わったときの直し忘れが起きる（set_role と同じ理由）。

    updated_by を持たせない理由:
        user_roles は「他人のロールを変える」操作なので、誰が変えたかを残す必要がある。
        パスワードは本人が自分の分を変えるのが基本で、
        user_id を見れば誰の話かが分かるため、変更者を別に持つ意味が薄い。
        admin による強制上書き（Issue #123）が入る段階で、
        本人以外が変えうるようになったら必要に応じて足す。

    セキュリティ:
        user_id はリクエスト由来になりうる値なので、
        必ず %s プレースホルダでバインドする（SQL文に文字列連結しない）。
        こうしておけば、値に何が入っていてもSQLとして解釈されない。
        また、失敗時にも password の値を例外メッセージやログに出さない。
    """
    # 平文はここでハッシュに変える。以降の処理は平文を扱わない
    password_hash = hash_password(password)

    # 記録時刻を文字列で用意（DBのカラムはTEXT型なのでISO形式の文字列で持つ）
    updated_at = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO user_passwords (user_id, password_hash, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                password_hash = excluded.password_hash,
                updated_at = excluded.updated_at
            """,
            (user_id, password_hash, updated_at),
        )
        conn.commit()
