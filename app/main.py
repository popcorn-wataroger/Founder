import csv
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Founder", version="0.1.0")

# 静的ファイル配信
app.mount("/static", StaticFiles(directory="static"), name="static")

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


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """チャットAPIエンドポイント（モック）"""
    # 空メッセージチェック
    if not req.message:
        return {"success": False, "message": "メッセージを入力してください"}
    return {"success": True, "reply": "（モック返答）AIがここで答えます。"}

class LoginRequest(BaseModel):
    employee_code: str
    password: str


@app.post("/api/login")
async def login(req: LoginRequest):
    """ログインAPIエンドポイント"""
    # 空欄チェック
    if not req.employee_code or not req.password:
        return {"success": False, "message": "社員コードとパスワードを入力してください"}

    # CSVからユーザーを検索
    user = get_user_by_employee_code(req.employee_code)

    # ユーザーが存在しない場合
    if user is None:
        return {"success": False, "message": "社員コードまたはパスワードが正しくありません"}

    # パスワードチェック
    if user["password"] != req.password:
        return {"success": False, "message": "社員コードまたはパスワードが正しくありません"}

    # ログイン成功
    return {"success": True, "role": user["role"], "name": user["name"]}

@app.get("/api/admin/users")
async def get_admin_users():
    """管理者用スタッフ一覧APIエンドポイント"""
    result = []
    for user in users:
        # ADMINは除外する
        if user["role"] == "admin":
            continue
        # パスワードを含めずに必要な項目だけ返す
        result.append({
            "user_id": user["user_id"],
            "employee_code": user["employee_code"],
            "name": user["name"],
            "department": user["department"],
            "employment_type": user["employment_type"],
        })
    return result

@app.get("/")
async def root():
    return FileResponse("static/index.html")