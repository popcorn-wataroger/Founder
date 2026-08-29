from contextlib import closing

from app.database import USERS_CSV_PATH, get_connection
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
    "get_user_by_employee_code",
    "get_user_by_id",
    "get_all_users",
    "resolve_role",
    "format_user_profile",
    "PROFILE_FIELD_LABELS",
]


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
