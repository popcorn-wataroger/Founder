import csv
from pathlib import Path

from app.user_roles import get_role

# CSVファイルのパス
USERS_CSV_PATH = Path("data/users.csv")

# 起動時にCSVを読み込んでメモリに保持
users: list[dict] = []
with open(USERS_CSV_PATH, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    users = list(reader)


def get_user_by_employee_code(employee_code: str) -> dict | None:
    """社員コードでユーザーを1件取得する"""
    for user in users:
        if user["employee_code"] == employee_code:
            return user
    return None


def get_user_by_id(user_id: str) -> dict | None:
    """user_idでユーザーを1件取得する"""
    for user in users:
        if user["user_id"] == user_id:
            return user
    return None


def resolve_role(user: dict) -> str:
    """その社員の実効ロールを返す。DBの上書きがあればそれを、無ければCSVのroleを返す。

    入力:
        user … users.csv の1行分の辞書（get_user_by_employee_code などの戻り値）。
                user_id と role を持っている前提

    処理:
        1. user_roles テーブルを user_id で引く（app/user_roles.py の get_role）
        2. 上書きがあれば（None でなければ）その値を返す
        3. 上書きが無ければ users.csv の role を返す

    出力:
        実際に権限判定へ使うロール名の文字列（employee / source_manager / admin）

    なぜ「DBを正、CSVを既定値」とするのか:
        users.csv はGit管理下にあり、初期状態を決めるための読み取り専用のファイル。
        運用中にロールを変えるたびにCSVを書き戻すと差分が出て、
        デプロイのたびにリポジトリの内容で上書きされてしまう。
        そこで「変更は必ずDBに積む」形にし、CSVは
        まだ一度も変更されていない社員の既定値として使う。
        優先順位を逆にする（CSVを正にする）と、DBに記録した変更が
        いつまでも効かず、変更した本人から見て何も起きていないように見える。

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

    # 一度も変更されていない社員は、CSVに書いてある初期値をそのまま使う
    role: str = user["role"]
    return role


# AIに渡してよい基本情報の項目と、その日本語ラベル。
# 「渡す項目を1つずつ書き出す（ホワイトリスト方式）」にしてあるので、
# users.csv に列が増えても、ここに書いていない列は自動的にAIへ渡らない。
#
# password と role を入れてはいけない理由:
#     password はそのままAIの回答文に出てくる恐れがある。
#     role は業務上の質問に無関係な内部の区分で、渡す理由がない。
# last_login_at を入れていない理由:
#     users.csv の列は使っておらず、実際の値は user_logins テーブルが持つ
#     （app/user_logins.py 参照）。このモジュールはCSVしか見ないため、ここから読むと常に空になる。
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
        user … users.csv の1行分の辞書（get_user_by_id などの戻り値）

    出力:
        「ラベル: 値」を改行で並べた文字列。
        例: "氏名: 奥村仁哉\\n社員コード: EMP001\\n部署: 営業部\\n..."
        渡せる項目が1つも無い場合は空文字を返す。

    処理:
        PROFILE_FIELD_LABELS を上から順に見て、値が入っている項目だけを行にする。

    値が空の項目を飛ばす理由:
        users.csv の家族構成は空欄の社員がいる。「家族構成: 」という行を渡すと、
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
