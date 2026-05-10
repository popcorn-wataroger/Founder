import csv
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

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


@app.get("/")
async def root():
    return FileResponse("static/index.html")