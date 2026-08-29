from contextlib import closing

import psycopg

from app.database import (
    USER_COLUMNS,
    USER_ID_SEQUENCE,
    USERS_CSV_PATH,
    USERS_EMPLOYEE_CODE_UNIQUE,
    get_connection,
)
from app.user_roles import get_role

# このモジュールが外部へ公開する名前。
# USERS_CSV_PATH（社員マスタの初期データCSVのパス）は app/database.py の定義を再公開する。
#
# 実体を database.py に置いている理由:
#     このモジュールは get_connection() を使うため app.database を import する。
#     逆向きに database.py が users.py を import すると相互参照になって起動できない。
#     初期投入は database.py の init_db() が行うので、パスの定義もそちらに置き、
#     ここでは名前だけ引き継ぐ。
#     「社員マスタのCSVの場所は app.users で分かる」という
#     これまでの使い方（tests/test_last_login.py など）をそのまま残すため。
__all__ = [
    "USERS_CSV_PATH",
    "EmployeeCodeAlreadyExistsError",
    "get_user_by_employee_code",
    "get_user_by_id",
    "get_all_users",
    "next_user_id",
    "create_user",
    "resolve_role",
    "format_user_profile",
    "PROFILE_FIELD_LABELS",
]


class EmployeeCodeAlreadyExistsError(Exception):
    """登録しようとした社員コードが既に使われているときに送出する例外。

    なぜ専用の例外クラスを作るか:
        create_user() が失敗する理由は「社員コードの重複」だけではない
        （接続断など、psycopg が投げる例外は他にもある）。
        呼び出し元（app/main.py のアカウント追加API）は
        重複のときだけ409を返し、それ以外はそのまま500にしたい。
        重複だけを名前の付いた例外にしておけば、except で1つだけ拾えばよく、
        エラーメッセージの文字列を見て分岐するような壊れやすい判定を書かずに済む。

    属性:
        employee_code … 重複した社員コード。呼び出し元が応答メッセージに使う
    """

    def __init__(self, employee_code: str) -> None:
        super().__init__(f"社員コード {employee_code} は既に使われています")
        self.employee_code = employee_code


def get_user_by_employee_code(employee_code: str) -> dict | None:
    """社員コードでユーザーを1件取得する。

    入力:
        employee_code … 探したい社員の社員コード（例: EMP001）。ログイン画面の入力値

    処理:
        users テーブルを employee_code で1行引く。

    出力:
        社員1人分の辞書（users テーブルの1行そのまま）。見つからなければ None

    なぜ SELECT * なのか:
        呼び出し元（ログイン処理）はパスワードの照合に password 列を使い、
        社員データ画面の表示には別の列を使う。必要な列は呼び出し元ごとに違うため、
        ここでは行をそのまま返し、「外に出してよい項目」の絞り込みは
        返した先（app/main.py のホワイトリスト）が行う。
        これはCSVの1行をそのまま返していたときと同じ役割分担で、
        今回のDB移行で振る舞いを変えないための選択でもある。

    セキュリティ:
        employee_code はログイン画面から来る値なので、必ず %s でバインドする
        （SQL文に文字列連結しない）。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE employee_code = %s",
            (employee_code,),
        ).fetchone()

    if row is None:
        return None
    return dict(row)


def get_user_by_id(user_id: str) -> dict | None:
    """user_idでユーザーを1件取得する。

    入力:
        user_id … 探したい社員の user_id

    処理:
        users テーブルを user_id で1行引く。

    出力:
        社員1人分の辞書（users テーブルの1行そのまま）。見つからなければ None

    セキュリティ:
        user_id はURLパス由来の値になりうるので、必ず %s でバインドする。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    if row is None:
        return None
    return dict(row)


def get_all_users() -> list[dict]:
    """全社員の行を返す。

    入力: なし
    処理: users テーブルを全件、user_id の順に取り出す
    出力: 社員1人分の辞書のリスト（1人も居なければ空のリスト）

    どこで使うか:
        スタッフ一覧（app/main.py の GET /api/admin/users）だけ。
        一覧に出すかどうかの絞り込み（ceo / admin を外す）と、
        画面に出す項目の絞り込みは呼び出し元が行う。

    なぜモジュール変数 users を廃止して関数にしたか:
        以前は起動時に読んだCSVの内容をモジュール変数に持っていた。
        DBに移すと、社員の追加やロール変更が起動後にも起こりうるため、
        変数に抱えたままだと古い内容を返し続ける。
        呼ばれるたびにDBを見る関数にしておけば、常にその時点の内容になる。

    並び順について:
        user_id は TEXT のため、並びは文字列としての順序になる。
        現在の社員は user_id が1桁（1〜9）なので data/users.csv と同じ並びになるが、
        10人目以降が増えると 1, 10, 2 … の順になる。
        画面からアカウントを作れるようになる段階で、採番と並び順をまとめて決める。
    """
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY user_id").fetchall()

    return [dict(row) for row in rows]


def next_user_id() -> str:
    """新しい社員に振る user_id を1つ取り出す。

    入力: なし
    処理: users_user_id_seq シーケンスを1つ進め、その値を文字列にする
    出力: まだ誰にも使われていない user_id（例: "10"）

    なぜシーケンスから取るのか（重要）:
        アカウント追加が2件同時に来ても、同じ番号を2人に渡さないため。
        nextval() は値を進めて返すところまでをDB側でアトミックに行うので、
        「2人が同じ値を読む」という状態自体が起こらない。
        SELECT MAX(user_id) + 1 で決めると、同時に読んだ2人が同じ番号になり、
        あとから入れた方が主キー違反で失敗する。

    なぜ文字列にするのか:
        user_id 列は TEXT で、JWT・URLパス・他テーブル（user_logins など）でも
        文字列として扱っている。ここで型を変えると比較が食い違う。

    採番だけを取り出しても行は増えない:
        この関数は番号を配るだけで、users には何も書かない。
        取ったあとに INSERT が失敗すると、その番号は欠番になる。
        欠番が出ても困らない（連番であることに意味を持たせていない）ので、
        取り消しの仕組みは持たない。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(f"SELECT nextval('{USER_ID_SEQUENCE}') AS user_id").fetchone()
        conn.commit()

    # nextval は必ず1行返る。返らなければDB側の異常なので、そのまま例外にする
    if row is None:
        raise RuntimeError("user_id の採番に失敗しました")

    return str(row["user_id"])


def create_user(employee_code: str, name: str, role: str, user_id: str) -> None:
    """社員マスタに1行追加する。

    入力:
        employee_code … 新しい社員の社員コード（ログインに使う。全社で一意）
        name          … 氏名
        role          … ロール名（employee / source_manager / ceo / admin）
        user_id       … 振る user_id。next_user_id() で取った値を渡す

    処理:
        users へ1行 INSERT する。指定のない列（部署・生年月日・家族構成など）は
        すべて空文字で入れる。

    出力:
        なし（副作用として users の行が1つ増える）

    例外:
        EmployeeCodeAlreadyExistsError … employee_code が既に使われているとき

    password 列を空文字で入れる理由（重要）:
        パスワードは user_passwords テーブルにハッシュで持つ（app/user_passwords.py）。
        users.password は data/users.csv から引き継いだ移行用の列で、
        ハッシュがまだ無い社員のログインにだけ使われる。
        新しいアカウントは最初からハッシュを持つので、この列に値を入れる理由がない。
        空文字にしておけば、万一ハッシュの保存に失敗しても
        平文の照合が「空文字と入力の一致」になり、誰もログインできない
        （ログインAPIは空のパスワードを400で弾くため、空同士の一致も起こらない）。

    ロール名をここで検証しない理由:
        妥当なロール名かどうかは呼び出し元（APIの入口）が決める。
        set_role() と同じ方針で、この関数は渡された値をそのまま保存する。
        検証を両方に置くと、許すロールが増えたときの直し忘れが起きる。

    重複を「先に SELECT で確かめる」形にしない理由（重要）:
        SELECT と INSERT の間に別のリクエストが同じ社員コードを入れると、
        確認をすり抜けて重複した行ができる。
        判定はDBの一意制約1つに任せ、違反を捕まえて例外に変える。
        こうすれば、同時に何件来ても「先に入った1件だけが成功する」が必ず成り立つ。

    どの一意制約に違反したかを見分ける理由:
        users には user_id（主キー）と employee_code の2つの一意制約がある。
        どちらでも同じ例外（UniqueViolation）が飛ぶため、
        制約の名前を見ないと user_id の重複まで「社員コードの重複」として
        409 で返してしまう。名前が employee_code の側でなければそのまま投げ直し、
        想定外の失敗を500として表に出す。

    セキュリティ:
        employee_code / name / role / user_id はいずれもリクエスト由来になりうるので、
        必ず %s プレースホルダでバインドする（SQL文に文字列連結しない）。
    """
    # 指定のあった列だけを埋め、残りは空文字にする。
    # 列の並びは USER_COLUMNS ひとつから作るので、列が増えても書き漏らしが起きない
    values_by_column = {
        "user_id": user_id,
        "employee_code": employee_code,
        "name": name,
        "role": role,
    }
    column_list = ", ".join(USER_COLUMNS)
    placeholders = ", ".join(["%s"] * len(USER_COLUMNS))
    values = tuple(values_by_column.get(column, "") for column in USER_COLUMNS)

    with closing(get_connection()) as conn:
        try:
            conn.execute(
                f"INSERT INTO users ({column_list}) VALUES ({placeholders})",
                values,
            )
        except psycopg.errors.UniqueViolation as error:
            # 社員コードの重複だけを、呼び出し元が扱える例外に翻訳する。
            # それ以外の一意制約違反（user_id の重複など）は想定外なのでそのまま投げ直す
            if error.diag.constraint_name == USERS_EMPLOYEE_CODE_UNIQUE:
                raise EmployeeCodeAlreadyExistsError(employee_code) from error
            raise
        conn.commit()


def resolve_role(user: dict) -> str:
    """その社員の実効ロールを返す。

    user_roles テーブルの上書きがあればそれを、無ければ users テーブルの role を返す。

    入力:
        user … 社員1人分の辞書（get_user_by_employee_code などの戻り値。
                users テーブルの1行）。user_id と role を持っている前提

    処理:
        1. user_roles テーブルを user_id で引く（app/user_roles.py の get_role）
        2. 上書きがあれば（None でなければ）その値を返す
        3. 上書きが無ければ users テーブルの role を返す

    出力:
        実際に権限判定へ使うロール名の文字列（employee / source_manager / ceo）

    なぜ user_roles を優先するのか:
        users.role は data/users.csv から一度だけ投入された初期値で、
        運用中のロール変更は user_roles テーブルに積まれる
        （app/main.py の PUT /api/admin/users/{user_id}/role）。
        優先順位を逆にすると、記録した変更がいつまでも効かず、
        変更した本人から見て何も起きていないように見える。

        なお users が（CSVではなく）DBのテーブルになった今も、この2段構えは変えていない。
        「誰がいつ変えたか」を updated_at / updated_by として残せるのが user_roles 側だけで、
        users.role を直接書き換える形にすると変更の履歴が消えるため。
        統合するかどうかはアカウント管理の実装（Issue #123 の後続）でまとめて判断する。

    なぜ users.py に置くのか:
        「この社員のロールは何か」は社員マスタの読み取りであって、
        「そのロールで何ができるか」という権限判定とは別の話。
        判定は app/routers/auth_router.py の can_upload_common_source() に
        集約したままにして、こちらは値を1つに決めることだけを担当する。
    """
    # DBの上書きを先に見る（無ければ None が返る）
    overridden_role = get_role(user["user_id"])
    if overridden_role is not None:
        return overridden_role

    # 一度も変更されていない社員は、users テーブルに入っている初期値をそのまま使う
    role: str = user["role"]
    return role


# AIに渡してよい基本情報の項目と、その日本語ラベル。
# 「渡す項目を1つずつ書き出す（ホワイトリスト方式）」にしてあるので、
# users テーブルに列が増えても、ここに書いていない列は自動的にAIへ渡らない。
#
# password と role を入れてはいけない理由:
#     password はそのままAIの回答文に出てくる恐れがある。
#     role は業務上の質問に無関係な内部の区分で、渡す理由がない。
# last_login_at を入れていない理由:
#     users テーブルの列は使っておらず、実際の値は user_logins テーブルが持つ
#     （app/user_logins.py 参照）。users 側は常に空文字なので、ここから読んでも意味がない。
PROFILE_FIELD_LABELS: list[tuple[str, str]] = [
    ("name", "氏名"),
    ("employee_code", "社員コード"),
    ("department", "部署"),
    ("gender", "性別"),
    ("birth_date", "生年月日"),
    ("family", "家族構成"),
    ("hire_date", "入社日"),
    ("employment_type", "雇用形態"),
]


def format_user_profile(user: dict) -> str:
    """社員1人分の基本情報を、AIのプロンプトに埋め込める1つの文字列にする。

    入力:
        user … 社員1人分の辞書（get_user_by_id などの戻り値。users テーブルの1行）

    出力:
        「ラベル: 値」を改行で並べた文字列。
        例: "氏名: 奥村仁哉\\n社員コード: EMP001\\n部署: 営業部\\n..."
        渡せる項目が1つも無い場合は空文字を返す。

    処理:
        PROFILE_FIELD_LABELS を上から順に見て、値が入っている項目だけを行にする。

    値が空の項目を飛ばす理由:
        家族構成が空欄の社員がいる。「家族構成: 」という行を渡すと、
        AIが空欄を何かの値と解釈して不自然な回答を作りかねない。
        行ごと省けば、AIから見れば「その情報は与えられていない」状態になり、
        プロンプトの指示どおり「記載がありません」と答えてくれる。

    どこで使うか:
        社員データ画面の「このスタッフについてチャット」（POST /api/chat/staff-inquiry）だけ。
        通常のチャットでは使わない。社員が他人の生年月日や家族構成を
        AIから引き出せてしまい、権限ルールの趣旨に反するため。
    """
    lines = []
    for field, label in PROFILE_FIELD_LABELS:
        value = (user.get(field) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)
