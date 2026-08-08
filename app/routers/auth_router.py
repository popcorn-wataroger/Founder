from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app import config
from app.user_logins import record_login
from app.users import get_user_by_employee_code

router = APIRouter(prefix="/api", tags=["auth"])

# Bearerトークンを取り出す仕組み（auto_error=Falseで401を自分でコントロール）
security = HTTPBearer(auto_error=False)


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
    if credentials is None:
        raise HTTPException(status_code=401, detail="トークンがありません")
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


def require_admin(token: dict = Depends(verify_token)) -> dict:
    """管理者（社長）以外は403を返す。

    入力:
        token … verify_token が検証したJWTの中身（user_id / role を含む）

    出力:
        管理者なら token をそのまま返す。管理者でなければ 403 を投げる。

    使いどころ:
        ソースのアップロード・削除、他人のチャットログ閲覧など、
        社長だけに許す操作のエンドポイントに Depends(require_admin) を付ける。
        認証（verify_token）と権限（require_admin）をこのファイルにまとめておくことで、
        どのルーターからも同じ判定を使い回せる。
    """
    if token.get("role") != "admin":
        raise HTTPException(status_code=403, detail="管理者のみ操作できます")
    return token


class LoginRequest(BaseModel):
    employee_code: str
    password: str


@router.post("/login")
async def login(req: LoginRequest):
    """ログインAPIエンドポイント

    入力:
        req … リクエストボディ（LoginRequest）
            employee_code … 社員コード（例: EMP001 / ADMIN）
            password      … パスワード

    出力:
        いずれの場合もHTTPステータスは200で、辞書の success で成否を表す。

        成功時: {"success": True, "role": ロール（employee/admin）,
                 "name": 氏名, "token": JWTアクセストークン}
        失敗時: {"success": False, "message": 画面に出すエラーメッセージ}

        失敗時に token や role を返さないのは、認証できていない相手に
        ロールなどの情報を渡さないため。
        message は「社員コードまたはパスワードが正しくありません」で統一しており、
        どちらが間違っているかは伝えない（存在する社員コードを推測されないため）。

    処理:
        1. 入力チェック → 社員コードでユーザーを探す → パスワード照合
        2. 認証に成功した場合だけ、最終ログイン日時を user_logins に記録する
        3. JWTアクセストークンを返す

    最終ログインを「成功時だけ」記録する理由:
        失敗も記録すると、パスワードを間違えただけの試行や、
        存在しない社員コードでの試行まで「最終ログイン」に見えてしまうため。
        社員データ画面が知りたいのは「最後に入れたのはいつか」なので、
        トークンを発行する直前に1回だけ記録する。
    """
    if not req.employee_code or not req.password:
        return {"success": False, "message": "社員コードとパスワードを入力してください"}

    user = get_user_by_employee_code(req.employee_code)

    if user is None:
        return {"success": False, "message": "社員コードまたはパスワードが正しくありません"}

    if user["password"] != req.password:
        return {"success": False, "message": "社員コードまたはパスワードが正しくありません"}

    # 認証に成功したので、この社員の最終ログイン日時を更新する。
    # user_id はログインしてきた側に由来する値なので、
    # record_login の中で %s プレースホルダにバインドしている（SQL文へ埋め込まない）
    record_login(user["user_id"])

    token = create_access_token(user_id=user["user_id"], role=user["role"])

    return {"success": True, "role": user["role"], "name": user["name"], "token": token}
