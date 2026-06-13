from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.database import init_db
from app.routers import auth_router, stripe_router
from app.routers.auth_router import verify_token
from app.users import users


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Founder", version="0.1.0", lifespan=lifespan)

app.include_router(stripe_router.router)
app.include_router(auth_router.router)

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
async def get_admin_users(token: dict = Depends(verify_token)):
    """管理者用スタッフ一覧APIエンドポイント（要認証）"""
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


@app.get("/")
async def root():
    return FileResponse("static/index.html")
