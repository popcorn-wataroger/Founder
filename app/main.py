from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.database import init_db
from app.routers import auth_router, chat_router, sources_router
from app.routers.auth_router import ROLE_CEO, VALID_ROLES, require_ceo
from app.user_logins import get_last_login_at
from app.user_roles import set_role
from app.users import get_user_by_id, resolve_role, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Founder", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router.router)
app.include_router(sources_router.router)
# 管理者専用のソース一覧API（/api/admin/users/{user_id}/sources）
app.include_router(sources_router.admin_router)
app.include_router(chat_router.router)
# 管理者専用のチャット履歴API（/api/admin/users/{user_id}/chat-sessions）
app.include_router(chat_router.admin_router)

# 静的ファイル配信
app.mount("/static", StaticFiles(directory="static"), name="static")


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """チャットAPIエンドポイント（モック）"""
    if not req.message:
        return {"success": False, "message": "メッセージを入力してください"}
    return {"success": True, "reply": "（モック返答）AIがここで答えます。"}


@app.get("/api/admin/users")
async def get_admin_users(token: dict = Depends(require_ceo)):
    """管理者用スタッフ一覧APIエンドポイント（社長のみ）。

    権限について:
        require_ceo を付けているので、社員が叩くと403で拒否される。
        以前は verify_token だけだったため、ログインさえしていれば社員でも
        全社員の氏名・部署・雇用形態を取得できてしまっていた。
        スタッフ一覧は社長だけが見る画面なので、管理者チェックが必須。
    """
    result = []
    for user in users:
        if user["role"] == ROLE_CEO:
            continue
        result.append(
            {
                "user_id": user["user_id"],
                "employee_code": user["employee_code"],
                "name": user["name"],
                "department": user["department"],
                "employment_type": user["employment_type"],
            }
        )
    return result


@app.get("/api/admin/users/{user_id}")
async def get_admin_user_detail(user_id: str, token: dict = Depends(require_ceo)) -> dict[str, str]:
    """指定した社員1人分の基本情報を返す（社長専用）。

    入力:
        user_id … 見たい社員の user_id（URLパスで指定）
        token   … require_ceo が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        基本情報の辞書（社員コード・氏名・部署・性別・生年月日・家族構成・
        入社日・雇用形態・最終ログイン・実効ロール）

    処理:
        1. user_id で社員を1件探す。見つからなければ404
        2. role が ceo なら404（下記の理由）
        3. 最終ログイン日時を user_logins テーブルから取り出す
        4. 実効ロールを resolve_role で決める（DBの上書きがあればそれ、無ければCSVの値）
        5. 画面に出す項目だけを1つずつ書き出して返す

    なぜ最終ログインだけCSVではなくDBから読むか:
        users.csv の last_login_at 列は全員空のまま使っていない。
        ログインのたびに変わる値をGit管理下のCSVに書き戻すと差分が出るため、
        記録先を user_logins テーブルに分けている（app/user_logins.py 参照）。
        まだ一度もログインしていない社員は記録が無いので空文字を返し、
        画面側（static/js/admin.js の formatLastLogin）が「未記録」と表示する。

    なぜ dict(user) をそのまま返さないか（重要）:
        users.csv には password 列がある。user をそのまま返すと
        平文パスワードがAPIレスポンスに丸ごと乗ってしまう。
        「返す項目を1つずつ書き出す（ホワイトリスト方式）」にしておけば、
        将来CSVに列が増えても、書き出していない列は自動的に外に出ない。

    なぜ role を返すようになったか:
        社員データ画面でその社員のロールを表示し、変更するために必要になったため
        （変更は PUT /api/admin/users/{user_id}/role）。
        現在のロールが分からないと、画面は変更後の値を選ばせようがない。
        このAPIは require_ceo を付けた社長専用なので、
        社員が他人のロールを知る経路にはならない。
        なお、返すのは users.csv の role ではなく resolve_role の結果（実効ロール）。
        CSVの値をそのまま返すと、DBで上書きしたロールが画面に反映されず、
        「変更したのに変わっていない」ように見えてしまう。

    なぜ ceo を404にするか:
        スタッフ一覧（GET /api/admin/users）が ceo を除外しているため、
        詳細だけ引ける状態にすると一覧と挙動がずれる。
        「一覧に出ない人は詳細も見られない」で揃えておく。
    """
    user = get_user_by_id(user_id)

    # 存在しない user_id、または社長本人（スタッフ一覧に出ない人）は404で揃える
    if user is None or user["role"] == ROLE_CEO:
        raise HTTPException(status_code=404, detail="社員が見つかりません")

    # 最終ログインはCSVではなくDBが持つ。記録が無ければ空文字（画面は「未記録」表示）
    last_login_at = get_last_login_at(user_id) or ""

    # ロールはCSVの値ではなくDBの上書きを優先した「実効ロール」を返す
    role = resolve_role(user)

    # 画面に出す項目だけを明示的に書き出す（password は返さない）
    return {
        "user_id": user["user_id"],
        "employee_code": user["employee_code"],
        "name": user["name"],
        "department": user["department"],
        "gender": user["gender"],
        "birth_date": user["birth_date"],
        "family": user["family"],
        "hire_date": user["hire_date"],
        "employment_type": user["employment_type"],
        "last_login_at": last_login_at,
        "role": role,
    }


class RoleUpdateRequest(BaseModel):
    role: str


@app.put("/api/admin/users/{user_id}/role")
async def update_admin_user_role(
    user_id: str, req: RoleUpdateRequest, token: dict = Depends(require_ceo)
) -> dict[str, object]:
    """指定した社員のロールを変更する（社長専用）。

    入力:
        user_id … ロールを変える対象の user_id（URLパスで指定）
        req     … リクエストボディ（RoleUpdateRequest）
            role … 変更後のロール名（employee / source_manager / ceo）
        token   … require_ceo が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        {"success": True, "user_id": 対象のuser_id, "role": 変更後のロール}

    処理:
        1. role が妥当な値か検証する（VALID_ROLES に無ければ400）
        2. 対象社員が実在するか確認する（見つからなければ404）
        3. 自分自身への変更を拒否する（403）
        4. user_roles テーブルに保存する

    エラー:
        400 … 知らないロール名を指定した場合
        403 … 社員が叩いた場合（require_ceo が投げる）／自分自身を対象にした場合
        404 … 対象の user_id が存在しない場合

    権限について:
        require_ceo を付けているので、社員と source_manager が叩くと403で拒否される。
        ロールの付け替えは「誰が何を見られるか」を決める操作なので、
        共通ソースを登録できるだけの source_manager には許さない。

    いつから有効になるか（重要）:
        変更は対象ユーザーの「次回ログインから」有効になる。
        ロールはログイン時に発行するJWTへ焼き付けられ、発行後は書き換えられないため、
        すでにログイン中の相手が持っているトークンには反映されない
        （app/routers/auth_router.py の login を参照）。
        すぐに反映させたい場合は、対象者にログインし直してもらう必要がある。

    なぜロール名の集合を自前で持たないか:
        ロール名の定義は app/routers/auth_router.py の定数に集約している。
        こちらにも書くと、ロールが増えたときに片方だけ直して
        「保存はできるが権限判定が知らないロール」ができてしまう。

    なぜ get_admin_user_detail と違って ceo を404にしないか:
        あちらは「スタッフ一覧に出ない人は詳細も見られない」で揃えるために
        ceo を404にしている。
        こちらは社長が誰かを社長にする／社長から降ろすための操作なので、
        社長を対象にできなければ機能そのものが成り立たない。
        目的が違うので、ceo の扱いも意図的に変えている。
    """
    # 1. 知らないロール名を保存させない。
    #    ここを通してしまうと、権限判定が知らない値がDBに入り、
    #    その社員は「どの権限にも当てはまらない状態」でログインすることになる
    if req.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"role は {' / '.join(sorted(VALID_ROLES))} のいずれかを指定してください",
        )

    # 2. 実在しない user_id を保存させない（user_roles に幽霊の行が増えるのを防ぐ）
    if get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="社員が見つかりません")

    # 3. 自分自身のロールは変えられない。
    #    最後の社長が自分を employee に落とすと、誰もこのAPIを叩けなくなり、
    #    ロールを戻す手段がDBの直接操作しか無くなる（社長ゼロの事故）。
    #    変更先が ceo であっても一律で拒否するのは、判定を単純に保つため。
    #    「ceo → ceo なら実質何も変わらないので許す」といった例外を作ると、
    #    条件が増えるほど事故を防げているかの確認が難しくなる
    if user_id == token["user_id"]:
        raise HTTPException(status_code=403, detail="自分自身のロールは変更できません")

    # 4. 誰がいつ変えたかを一緒に残す（updated_by は変更した社長の user_id）
    set_role(user_id, req.role, updated_by=token["user_id"])

    return {"success": True, "user_id": user_id, "role": req.role}


@app.get("/")
async def root():
    return FileResponse("static/index.html")
