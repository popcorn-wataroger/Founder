import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

# ローカル開発では .env から環境変数を読み込む。
# 本番（Cloud Run）では .env を置かず、Secret Manager のシークレットを
# 環境変数としてマウントする。.env が無い場合 load_dotenv() は何もしないため、
# ローカルと本番でこの config の読み方は変わらない。
load_dotenv()

# アップロードしたファイルの保存先ディレクトリ。
# 保存側(sources_router)と読み込み側(vectorizer)の両方が「同じ1つの正解」を
# 参照できるよう、どちらからも依存しない config に置く。
UPLOAD_DIR: Final[Path] = Path("uploads")

# 実行環境の識別子。
# 未設定のときは「本番相当」として扱う（既定値を置かない）。
# 未設定をローカル扱いにすると、本番で APP_ENV を渡し忘れたときに
# 開発用の既定値で起動が通ってしまい、設定漏れに気づけないため。
# 危険側にフォールバックさせない、が原則。
# ローカルは .env の APP_ENV=local、CIは ci.yml の APP_ENV=test で明示している。
APP_ENV: Final[str] = os.getenv("APP_ENV", "")

# 開発用のゆるい既定値を許してよい環境。
# ここに含まれない値（未設定・production など）は本番相当として扱い、設定漏れを許さない。
DEV_ENVS: Final[frozenset[str]] = frozenset({"local", "test"})

# JWT
JWT_ALGORITHM: Final[str] = "HS256"
JWT_EXPIRE_HOURS: Final[int] = 8

# ローカル開発とテストでのみ使う署名鍵。本番では絶対に使わせない。
_DEV_JWT_SECRET_KEY: Final[str] = "dev-only-insecure-jwt-secret"


def _load_jwt_secret_key() -> str:
    """JWT の署名鍵を読み込む。本番相当の環境で未設定なら起動を止める。

    入力: 環境変数 JWT_SECRET_KEY と APP_ENV
    出力: 署名鍵の文字列
    例外: 本番相当の環境で JWT_SECRET_KEY が未設定・空のとき RuntimeError

    署名鍵を知っている人は管理者(ADMIN)のトークンを自作でき、
    全社員の個別ソースを閲覧できてしまう。
    そのため、以前あったような固定のフォールバック値は置かない
    （公開リポジトリに書かれた値は「誰でも知っている鍵」と同じ）。

    未設定でも起動できてしまうことが最も危険なため、本番相当では
    リクエストを受け付ける前にここで落とす。
    """
    secret_key = os.getenv("JWT_SECRET_KEY")
    if secret_key:
        return secret_key

    if APP_ENV in DEV_ENVS:
        return _DEV_JWT_SECRET_KEY

    # 鍵の値そのものはログに出さない（出すのは「設定あり/なし」だけ）
    raise RuntimeError(
        f"JWT_SECRET_KEY が設定されていません（APP_ENV={APP_ENV or '未設定'}）。"
        "本番相当の環境では開発用の既定値を使いません。"
        "Secret Manager のシークレットを環境変数としてマウントしてください。"
    )


JWT_SECRET_KEY: Final[str] = _load_jwt_secret_key()

# Gemini
GEMINI_API_KEY: Final[str] = os.getenv("GEMINI_API_KEY", "")

# Qdrant（ベクトルDB）の接続情報。値は .env から読み込む（コードに直書きしない）
QDRANT_URL: Final[str] = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY: Final[str] = os.getenv("QDRANT_API_KEY", "")
