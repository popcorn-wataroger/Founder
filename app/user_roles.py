"""ロールの上書き（user_roles テーブル）のDB操作をまとめたモジュール。

取得系（get_role / get_all_role_overrides）と書き込み系（set_role）だけを持つ。
get_all_role_overrides は一覧の画面のように全社員ぶんが要る場面のためのもので、
get_role を人数ぶん呼ぶ代わりに1回のクエリでまとめて引く。

なぜ data/users.csv ではなくDBに持つか:
    users.csv はGit管理下にあるため、運用中にロールを変えるたびに書き戻すと
    差分が出て `git status` が汚れる。CSV全体を書き換える方式は、
    失敗したときに他の社員の行まで壊すリスクもある。
    社員マスタ（滅多に変わらない）はCSVのまま読み取り専用にしておき、
    運用中に変わる値だけをこのテーブルに分けて持つ（user_logins と同じ考え方）。

このファイルに権限判定を書かない理由:
    ここはDBの読み書きだけを担当する。
    「そのロールで何ができるか」の判定は app/routers/auth_router.py の
    can_upload_common_source() に集約してあり、両方に書くと
    判定が2箇所に散って食い違う。役割を分けておけば、
    権限ルールが変わってもこのファイルは触らずに済む。

DB操作の流儀:
    with closing(get_connection()) as conn: で接続 → execute（%sプレースホルダ）→ commit
    （commit は with ブロックの内側で行い、close は with を抜けるときに任せる）

    closing を使う理由:
        execute の途中で例外が出ても、with を抜けるときに必ず接続が閉じられるため。
        Cloud SQL は同時接続数に上限があり、閉じ忘れが溜まると新規接続が拒否される。

日時の形式:
    user_logins.last_login_at / sources.uploaded_at と同じく、
    UTC・ISO形式の文字列（例: 2026-08-01T10:23:45.123456+00:00）で保存する。
    画面側は new Date() でブラウザのローカル時刻に変換して表示するため、
    オフセット（+00:00）を必ず含める必要がある。
"""

from contextlib import closing
from datetime import datetime, timezone

from app.database import get_connection


def get_role(user_id: str) -> str | None:
    """指定ユーザーの上書きされたロールを返す。上書きが無ければ None。

    入力:
        user_id … 調べたい社員の user_id（users.csv のID）

    処理:
        user_roles を user_id で1行引く。

    出力:
        上書きされたロール名の文字列。行が無ければ None

    None の意味:
        「ロールが無い」ではなく「上書きされていない」。
        呼び出し元は None のとき data/users.csv の role を使う、
        という組み立てになる。どちらを優先するかはこの関数では決めない。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT role FROM user_roles WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    # 一度も上書きされていない社員は行そのものが無い
    if row is None:
        return None

    role: str = row["role"]
    return role


def set_role(user_id: str, role: str, updated_by: str) -> None:
    """指定ユーザーのロールを記録する。すでに記録があれば上書きする。

    入力:
        user_id    … ロールを変える対象の社員の user_id
        role       … 記録するロール名（employee / source_manager / ceo）
        updated_by … 変更した人の user_id（誰が変えたかを残すため）

    処理:
        1. 現在時刻（UTC・ISO形式の文字列）を用意する
        2. user_roles に INSERT する。すでにその user_id の行があれば
           role / updated_at / updated_by を上書きする（UPSERT）
        3. commit して確定する

    出力:
        なし（副作用としてテーブルの行が増える・書き換わる）

    なぜ UPSERT（ON CONFLICT DO UPDATE）か:
        user_id を主キーにして「1社員1行」を保つため。
        「消してから入れ直す」方式と違って、更新対象はその user_id の1行だけになるので、
        他の社員の記録が書き換わることが構造的に起こらない。

    role の値をここで検証しない理由:
        妥当なロール名かどうかは呼び出し元（APIの入口）が決める。
        この関数は渡された値をそのまま保存する。
        検証を両方に置くと、許すロールが増えたときの直し忘れが起きる。

    セキュリティ:
        user_id / role / updated_by はいずれもリクエスト由来になりうる値なので、
        必ず %s プレースホルダでバインドする（SQL文に文字列連結しない）。
        こうしておけば、値に何が入っていてもSQLとして解釈されない。
    """
    # 記録時刻を文字列で用意（DBのカラムはTEXT型なのでISO形式の文字列で持つ）
    updated_at = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO user_roles (user_id, role, updated_at, updated_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                role = excluded.role,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (user_id, role, updated_at, updated_by),
        )
        conn.commit()


def get_all_role_overrides() -> dict[str, dict[str, str]]:
    """上書きされたロールを全社員ぶん、1回のクエリでまとめて返す。

    入力:
        なし

    処理:
        user_roles を全件取り出し、user_id をキーにした辞書に詰め替える。

    出力:
        {user_id: {"role": ロール名, "updated_at": 日時, "updated_by": 変更した人のuser_id}}
        の形の辞書。上書きが一度も無ければ空の辞書。
        キーに無い user_id は「一度も上書きされていない」という意味になり、
        get_role() が None を返すのと同じことを表す。

    get_role() と別に用意する理由（重要）:
        get_role() は1人ぶんを1回のクエリで引く。一覧の画面のように
        全社員ぶんが必要な場面でこれを人数ぶん呼ぶと、
        社員100人でDB接続が100回開く（Issue #127）。
        Cloud SQL は同時接続数に上限があるため、人数に比例して接続が増える書き方は
        人が増えたときに一覧ごと落ちる原因になる。
        全件が要る呼び出し元はこちらを1回だけ呼ぶ。

        1人ぶんの取得（ログイン時の実効ロール判定など）は
        引き続き get_role() を使う。全件を読み込んでから1件を取り出すのは無駄なため。

    全件を一度に読んで問題ないと判断した理由:
        user_roles は「1社員1行」で、行数は社員数を超えない（想定規模は100人以下）。
        履歴を積むテーブルではないので、時間が経っても行数は増え続けない。
    """
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT user_id, role, updated_at, updated_by FROM user_roles"
        ).fetchall()

    return {
        row["user_id"]: {
            "role": row["role"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }
        for row in rows
    }
