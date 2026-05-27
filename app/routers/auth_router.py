from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app import config
from app.users import get_user_by_employee_code

router = APIRouter(prefix="/api", tags=["auth"])

# Bearerトークンを取り出す仕組み
security = HTTPBearer()


def create_access_token(user_id: str, role: str) -> str:
    """JWTアクセストークンを生成する"""
    expire = datetime.now(timezone.utc) + timedelta(hours=config.JWT_EXPIRE_HOURS)
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, config.JWT_SECRET_KEY, algorithm=config.JWT_ALGORITHM)


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """JWTトークンを検証して中身を返す"""
    try:
        payload = jwt.decode(
            credentials.credentials,
            config.JWT_SECRET_KEY,
            algorithms=[config.JWT_ALGORITHM],
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="トークンの有効期限が切れています")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="無効なトークンです")


class LoginRequest(BaseModel):
    employee_code: str
    password: str


@router.post("/login")
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

    # JWTトークンを生成
    token = create_access_token(user_id=user["user_id"], role=user["role"])

    # ログイン成功
    return {"success": True, "role": user["role"], "name": user["name"], "token": token}
