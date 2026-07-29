from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.database import init_db
from app.routers import auth_router, chat_router, sources_router
from app.routers.auth_router import require_admin
from app.users import get_user_by_id, users


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
async def get_admin_users(token: dict = Depends(require_admin)):
    """管理者用スタッフ一覧APIエンドポイント（社長のみ）。

    権限について:
        require_admin を付けているので、社員が叩くと403で拒否される。
        以前は verify_token だけだったため、ログインさえしていれば社員でも
        全社員の氏名・部署・雇用形態を取得できてしまっていた。
        スタッフ一覧は社長だけが見る画面なので、管理者チェックが必須。
    """
    result = []
    for user in users:
        if user["role"] == "admin":
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
async def get_admin_user_detail(
    user_id: str, token: dict = Depends(require_admin)
) -> dict[str, str]:
    """指定した社員1人分の基本情報を返す（社長専用）。

    入力:
        user_id … 見たい社員の user_id（URLパスで指定）
        token   … require_admin が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        基本情報の辞書（社員コード・氏名・部署・性別・生年月日・家族構成・
        入社日・雇用形態・最終ログイン）

    処理:
        1. user_id で社員を1件探す。見つからなければ404
        2. role が admin なら404（下記の理由）
        3. 画面に出す項目だけを1つずつ書き出して返す

    なぜ dict(user) をそのまま返さないか（重要）:
        users.csv には password 列がある。user をそのまま返すと
        平文パスワードがAPIレスポンスに丸ごと乗ってしまう。
        「返す項目を1つずつ書き出す（ホワイトリスト方式）」にしておけば、
        将来CSVに列が増えても、書き出していない列は自動的に外に出ない。

    なぜ admin を404にするか:
        スタッフ一覧（GET /api/admin/users）が admin を除外しているため、
        詳細だけ引ける状態にすると一覧と挙動がずれる。
        「一覧に出ない人は詳細も見られない」で揃えておく。
    """
    user = get_user_by_id(user_id)

    # 存在しない user_id、または管理者本人（スタッフ一覧に出ない人）は404で揃える
    if user is None or user["role"] == "admin":
        raise HTTPException(status_code=404, detail="社員が見つかりません")

    # 画面に出す項目だけを明示的に書き出す（password と role は返さない）
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
        "last_login_at": user["last_login_at"],
    }


@app.get("/")
async def root():
    return FileResponse("static/index.html")
