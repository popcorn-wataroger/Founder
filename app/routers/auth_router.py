import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app import config
from app.user_logins import record_login
from app.users import get_user_by_employee_code, resolve_role

router = APIRouter(prefix="/api", tags=["auth"])

logger = logging.getLogger(__name__)

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

        失敗になるのは次の3つ。
        - 社員コードかパスワードが未入力
        - 社員コードが見つからない、またはパスワードが一致しない
        - 実効ロールが VALID_ROLES に無い（user_roles テーブルに未知のロール名が
          記録されている場合など）
        最後のケースも message を認証失敗と同一にしているのは、
        「ロールが不正です」と伝えると内部の状態を外に漏らすことになるため。
        原因はサーバーのログ（logger.error）から追う。

    処理:
        1. 入力チェック → 社員コードでユーザーを探す → パスワード照合
        2. 認証に成功した場合だけ、最終ログイン日時を user_logins に記録する
        3. 実効ロールを決め、受け付けてよいロール名かを検証する
        4. JWTアクセストークンを返す

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

    # 実効ロールが「受け付けてよいロール名」かをここで確かめる。
    #
    # なぜ resolve_role() の中ではなく login() で検証するのか:
    #     resolve_role() は「実効ロールの値を1つに決める」ことだけを担当している。
    #     「そのロール名を受け付けてよいか」は権限の話で、判定は auth_router に
    #     集約してある（app/users.py の docstring にも明記している）。
    #     users.py で VALID_ROLES を参照すると users → routers の依存が生まれ、
    #     いまの routers → users という一方向の流れが逆流する。
    #     ここで弾いておけば、不正な値がJWTに焼き付く経路そのものが閉じる。
    #
    # どこから不正な値が入りうるか:
    #     user_roles テーブルの role 列。ロール名を改名したのに古い行が残っている、
    #     DBを直接書き換えた、といった場合に、権限判定が知らない値が返る。
    #     そのままトークンにすると「どの権限にも当てはまらない状態」でログインでき、
    #     以後の挙動が読めなくなる。
    if role not in VALID_ROLES:
        # ロール名は機密ではなく、改名時の取り残しを調べるのに要るのでログに残す。
        # ただしDB由来の値なので、改行を落としてから渡す（ログインジェクション対策。
        # 値に改行が混ざると、偽のログ行を丸ごと差し込まれて調査を欺かれる）
        #
        # CodeQL の py/clear-text-logging-sensitive-data について（誤検知と判断している）:
        #     検出された経路（PR #103 の alert #22 / #23 で確認）
        #         Source … 上の get_user_by_employee_code() の戻り値
        #         Sink   … このログ出力に渡す user["user_id"] と safe_role
        #
        #     なぜ汚染扱いになるか:
        #         get_user_by_employee_code() が返すのは data/users.csv の1行そのままで、
        #         その辞書には password 列が含まれる。CodeQL は辞書全体を機密と見なすため、
        #         そこから添字で取り出した値は、中身に関係なく機密として追跡される。
        #         scripts/dev.py でシークレット名を print しないことにしたのと同じ現象
        #         （あちらも SECRET_NAMES は定数だが、fetch_secret() へ渡す値の
        #         供給元として汚染された）。
        #
        #     なぜ誤検知と判断できるか:
        #         実際にログへ出るのは user_id（1〜8 の内部連番）とロール名だけで、
        #         password の値は経路に一度も現れない。
        #
        #     なぜ値を落とさないか:
        #         ロール改名時の取り残しを調べるには、どの社員にどの値が残っているかが必要。
        #         値を落とすと user_roles テーブルを直接SELECTしないと調査できず、
        #         このログを置いた目的（気づける形にする）が果たせない。
        #
        #     インライン抑制コメントは置かない。行末・直前の独立行のどちらでも
        #     GitHub CodeQL Action 側で効かなかった前例があるため、
        #     アラートは GitHub 上で dismiss する
        #     （app/storage.py と app/upload_paths.py の py/path-injection と同じ扱い）。
        safe_role = str(role).replace("\r", "").replace("\n", "")
        logger.error(
            "未知のロールが解決されたためログインを拒否しました user_id=%s role=%s",
            user["user_id"],
            safe_role,
        )
        # 利用者へのメッセージは通常の認証失敗と同じにする。
        # 「ロールが不正です」と伝えると、内部の状態を外に漏らすことになる
        return {"success": False, "message": "社員コードまたはパスワードが正しくありません"}

    token = create_access_token(user_id=user["user_id"], role=role)

    return {"success": True, "role": role, "name": user["name"], "token": token}
