import logging
import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

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
# ローカルは scripts/dev.py が起動前に APP_ENV=local を設定し、
# CIは ci.yml の APP_ENV=test で明示している（どちらも未設定にならない）。
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

# Qdrant（ベクトルDB）の接続情報。値は環境変数から読み込む（コードに直書きしない）
QDRANT_URL: Final[str] = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY: Final[str] = os.getenv("QDRANT_API_KEY", "")

# Google Cloud Storage のバケット名。
# 設定されていればGCSへ、空ならローカルの UPLOAD_DIR へ保存する
# （どちらを使うかの判断は app/storage.py に集約している）。
#
# 認証情報（GOOGLE_APPLICATION_CREDENTIALS）をここで読まない理由:
#     google-cloud-storage のSDKが同名の環境変数を自分で読む。
#     config でも読むと「正解が2箇所にある」状態になり、片方だけ変えたときにずれる。
#     .env.example には引き続き記載しておき、設定する場所だけ伝える。
GCS_BUCKET_NAME: Final[str] = os.getenv("GCS_BUCKET_NAME", "")

# Cloud SQL(PostgreSQL) への接続URL。値は環境変数から読み込む（コードに直書きしない）。
#
# 接続先は実行環境によって形が変わる:
#     ローカル: Cloud SQL Auth Proxy を起動し、proxy が待ち受ける localhost へ繋ぐ
#               （例: postgresql://ユーザー:パスワード@localhost:5432/DB名）
#     Cloud Run: Unix ソケット経由で繋ぐ。ホスト名に /cloudsql/接続名 を指定する
#               （例: postgresql://ユーザー:パスワード@/DB名?host=/cloudsql/接続名）
# どちらもURLの文字列が違うだけなので、config はURLをそのまま受け取るだけにして
# 環境ごとの分岐を持たない（分岐を持つと環境が増えるたびにここが膨らむ）。
#
# 未設定判定をここに置かず app/database.py に任せる理由:
#     GCS_BUCKET_NAME と同じ考え方で、「未設定のときどう振る舞うか」は
#     接続を実際に張るモジュールの責務。config は値を読むだけに留める。
DATABASE_URL: Final[str] = os.getenv("DATABASE_URL", "")

# 本番相当で未設定だと機能が動かなくなるが、起動は止めない設定。
# JWT_SECRET_KEY と違い、漏洩ではなく可用性の問題なので扱いを分けている。
#
# GCS_BUCKET_NAME をここに入れない理由:
#     こちらは警告では済まない。本番相当でバケット名が無いままローカル保存に
#     倒れると、アップロードは成功したように見えてコンテナ停止時にファイルが消える。
#     そのため app/storage.py の _resolve_backend が import 時に RuntimeError で
#     起動を止める（JWT_SECRET_KEY と同じ「危険側に倒さない」扱い）。
#     判定を storage 側に置くのは、保存先の知識をあのモジュールへ閉じ込めるため。
_OPTIONAL_PROD_SETTINGS: Final[tuple[tuple[str, str], ...]] = (
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("QDRANT_URL", QDRANT_URL),
    ("QDRANT_API_KEY", QDRANT_API_KEY),
)


def _warn_missing_optional_settings() -> None:
    """本番相当の環境で、未設定のまま起動する設定を警告する。

    入力: モジュール変数 APP_ENV と _OPTIONAL_PROD_SETTINGS
    処理: 本番相当かつ空の項目を集めて logger.warning を1回出す
    出力: なし（副作用としてログが出るだけ。起動は止めない）

    これらが空でもアプリは起動してしまう。そのため本番で Secret Manager の
    マウントを忘れると「起動はするが検索と回答が全部失敗する」状態になり、
    ログを見ても原因が設定漏れだと気づきにくい。
    起動時点で名前を出しておけば、最初に見るログで原因にたどり着ける。

    JWT_SECRET_KEY のように起動を止めないのは、これが漏洩ではなく可用性の
    問題だから。止めると、一部機能のためにサービス全体が上がらなくなる。

    値そのものは出さない（出すのは「未設定である」ことと変数名だけ）。
    """
    if APP_ENV in DEV_ENVS:
        return

    missing = [name for name, value in _OPTIONAL_PROD_SETTINGS if not value]
    if not missing:
        return

    logger.warning(
        "本番相当の環境で未設定の項目があります（APP_ENV=%s）: %s. "
        "Secret Manager のシークレットが環境変数としてマウントされているか確認してください。"
        "起動は続行しますが、該当する機能は失敗します。",
        APP_ENV or "未設定",
        ", ".join(missing),
    )


_warn_missing_optional_settings()
