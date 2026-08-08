"""ローカル開発用の起動スクリプト。

機密情報の「正解」を Google Secret Manager から取得し、環境変数に入れてから
アプリ（uvicorn）を起動する。app/config.py 側は今までどおり os.getenv() で
読むだけなので、本番（Cloud Run が環境変数としてマウントする）と読み方が変わらない。

前提（初回だけ）:
  1. gcloud auth application-default login
  2. uv add --dev google-cloud-secret-manager

使い方:
  uv run python scripts/dev.py
"""

from __future__ import annotations

import os
import sys
from typing import Final

from google.api_core import exceptions as gcp_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import secretmanager

PROJECT_ID: Final[str] = "notebooklm-482403"

# Secret Manager から取得して環境変数に入れる名前の一覧。
# シークレット名と環境変数名はあえて同じにしている（対応表を持たずに済むため）。
SECRET_NAMES: Final[tuple[str, ...]] = (
    "JWT_SECRET_KEY",
    "GEMINI_API_KEY",
    "QDRANT_URL",
    "QDRANT_API_KEY",
)


def fetch_secret(client: secretmanager.SecretManagerServiceClient, name: str) -> str:
    """Secret Manager から最新バージョンの値を1件取得する。

    入力: Secret Manager のクライアント、シークレット名
    出力: シークレットの値（文字列）
    例外: 権限が無い / シークレットが存在しない場合は RuntimeError
    """
    path = f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": path})
    except gcp_exceptions.PermissionDenied as exc:
        raise RuntimeError(
            f"{name} を読む権限がありません。"
            "roles/secretmanager.secretAccessor が付与されているか確認してください。"
        ) from exc
    except gcp_exceptions.NotFound as exc:
        raise RuntimeError(
            f"{name} が Secret Manager に存在しません（プロジェクト: {PROJECT_ID}）。"
        ) from exc
    return response.payload.data.decode("utf-8")


def load_secrets_into_env() -> None:
    """SECRET_NAMES の各値を取得し、環境変数として設定する。

    入力: なし（モジュール定数 PROJECT_ID / SECRET_NAMES を使う）
    処理: Secret Manager から取得して os.environ に入れる
    出力: なし（副作用として os.environ が更新される）
    例外: 認証情報が無い場合は RuntimeError
    """
    try:
        client = secretmanager.SecretManagerServiceClient()
    except auth_exceptions.DefaultCredentialsError as exc:
        raise RuntimeError(
            "GCP の認証情報が見つかりません。"
            "'gcloud auth application-default login' を実行してください。"
        ) from exc

    for name in SECRET_NAMES:
        os.environ[name] = fetch_secret(client, name)

    # 値そのものは絶対に出さない。出してよいのは「何件取得できたか」だけ。
    #
    # シークレット名を出力に含めない理由:
    # CodeQL(py/clear-text-logging-sensitive-data) はデータの流れを追う。
    # SECRET_NAMES は定数だが fetch_secret() に渡す値の供給元であるため、
    # 「機密に関係する値」として汚染マークが付く。名前を print すると、
    # 実際に出しているのが名前だけでも検出対象になる。
    # len() の結果は整数なので、文字列としての汚染は伝播しない。
    #
    # 動作確認には「4件取得できた」ことが分かれば十分。
    # 途中で失敗した場合は、どのシークレットで止まったかを
    # fetch_secret() のエラーメッセージが持っている。
    print(f"{len(SECRET_NAMES)} 件のシークレットを取得しました")


def main() -> int:
    """シークレットを読み込んでから uvicorn を起動する。

    入力: なし
    処理: 環境変数を整えて uvicorn にプロセスを差し替える
    出力: 終了コード（失敗時のみ 1 を返す。成功時は uvicorn に置き換わるため返らない）
    """
    try:
        load_secrets_into_env()
    except RuntimeError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    os.environ["APP_ENV"] = "local"

    # 現在のプロセスを uvicorn に置き換える（Ctrl+C がそのまま uvicorn に届く）
    os.execvp("uv", ["uv", "run", "uvicorn", "app.main:app", "--reload"])


if __name__ == "__main__":
    sys.exit(main())
