from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app import config
from app.user_logins import record_login
from app.users import get_user_by_employee_code, resolve_role

router = APIRouter(prefix="/api", tags=["auth"])

# Bearerトークンを取り出す仕組み（auto_error=Falseで401を自分でコントロール）
security = HTTPBearer(auto_error=False)

# ロールを表す定数。
# 文字列を直接書くとタイプミスに気づけない（"admin" と "Admin" の違いなど）ため、
# ロール名はここに集約して、判定側はこの定数を参照する。
ROLE_ADMIN = "admin"
ROLE_SOURCE_MANAGER = "source_manager"
ROLE_EMPLOYEE = "employee"

# 受け付けてよいロール名の全体。ロールを増やすときはここに足せば、
# 検証している側（ロール変更API）は触らずに済む
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_SOURCE_MANAGER, ROLE_EMPLOYEE})


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
    if token.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="管理者のみ操作できます")
    return token


def can_upload_common_source(role: str) -> bool:
    """全社共通ソースをアップロードできる役割かどうかを判定する。

    入力:
        role … ロール名の文字列（例: "admin" / "source_manager" / "employee"）。
                JWTの role や users.csv の role をそのまま渡す想定。

    処理:
        role が ROLE_ADMIN か ROLE_SOURCE_MANAGER のいずれかに一致するかを調べる。
        一致しないもの（未知のロール名や空文字を含む）はすべて許可しない側に倒す。

    出力:
        アップロードを許可してよいなら True、それ以外は False。
    """
    return role in (ROLE_ADMIN, ROLE_SOURCE_MANAGER)


def require_source_uploader(token: dict = Depends(verify_token)) -> dict:
    """全社共通ソースをアップロードできる権限がなければ403を返す。

    入力:
        token … verify_token が検証したJWTの中身（user_id / role を含む）

    処理:
        token から role を取り出し、can_upload_common_source() で可否を判定する。

    出力:
        権限があれば token をそのまま返す。なければ 403 を投げる。

    判定を can_upload_common_source() に分けている理由:
        「誰がその権限を持つか（役割の持ち方）」と
        「どこでその権限を要求するか（FastAPIの依存関係としての使い方）」を
        分離しておくため。
        いまは role という1つの文字列で役割を表しているが、将来これを
        権限フラグ（can_upload_source のような列）に変えることになっても、
        差し替えるのは can_upload_common_source() の中身だけで済み、
        この関数やエンドポイント側は触らずに済む。
        vector_store.search() が検索範囲の権限判定を一点に集約しているのと同じ考え方。
    """
    if not can_upload_common_source(token.get("role", "")):
        raise HTTPException(status_code=403, detail="共通ソースをアップロードする権限がありません")
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

        成功時: {"success": True, "role": 実効ロール（employee/source_manager/admin）,
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

    # 実効ロールをここで1回だけ決める（DBの上書きがあればそれ、無ければCSVの値）。
    #
    # なぜログイン時に解決するのか:
    #     JWTは発行したあとに中身を書き換えられない（書き換えると署名が合わなくなる）。
    #     つまりロールはトークンに焼き付けられ、有効期限が切れるまでそのまま使われる。
    #     ロールを変更しても、すでに配ったトークンには反映されない
    #     ＝「次回ログインから有効になる」という仕様になる。
    #     リクエストのたびにDBを引いて即時反映させることもできるが、
    #     全APIがログインユーザーの分だけDBアクセスを増やすことになるため、
    #     MVPでは反映の速さより単純さを取る。
    #
    # token とレスポンスで同じ role を使う理由:
    #     レスポンスの role は画面の遷移先の判定（社員画面か管理者画面か）に使われ、
    #     token の role はAPI側の権限判定に使われる。
    #     この2つが食い違うと、管理者画面へ遷移したのにAPIが403を返す、
    #     という噛み合わない状態になる。必ず同じ値を渡す
    role = resolve_role(user)

    token = create_access_token(user_id=user["user_id"], role=role)

    return {"success": True, "role": role, "name": user["name"], "token": token}
