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

# grpc の DNS 解決を OS 側に任せる。
#
# grpc は既定で内蔵リゾルバ(c-ares)を使うが、DNS サーバーが IPv6 の
# リンクローカルアドレス(fe80::...%en0)しか無い環境では、末尾のスコープID
# (%en0)を解釈できず "Could not contact DNS servers" で失敗する。
# native を指定すると OS の名前解決を使うため、この環境でも解決できる。
#
# grpc は import 時にこの設定を読むため、google.cloud のインポートより
# 前に設定する必要がある。そのため import の位置を意図的に分けている。
os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

import sys  # noqa: E402
from typing import Final  # noqa: E402

from google.api_core import exceptions as gcp_exceptions  # noqa: E402
from google.auth import exceptions as auth_exceptions  # noqa: E402
from google.cloud import secretmanager  # noqa: E402

PROJECT_ID: Final[str] = "notebooklm-482403"

# Secret Manager から取得して環境変数に入れる名前の一覧。
# シークレット名と環境変数名はあえて同じにしている（対応表を持たずに済むため）。
#
# DATABASE_URL をここに入れていない理由（Issue #65 / #66）:
#     Secret Manager への登録は Issue #66（Cloud Run デプロイ）で完了しており、
#     バージョン1が有効になっている。それでもこの一覧に加えていないのは、
#     登録されている値が Cloud Run 用の形式だから。
#
#         登録値（Cloud Run）: postgresql://...@/founder?host=/cloudsql/<接続名>
#         ローカルで必要な値  : postgresql://...@localhost:5432/founder
#
#     Cloud Run は Unix ソケット経由で繋ぐが、ローカルは cloud-sql-proxy が
#     待ち受ける localhost へ繋ぐ。ここに加えて取得しても、ローカルでは接続できない。
#
#     ローカル用の値を別シークレットとして持たせる案もあるが、
#     起動手順が変わって動作確認をやり直すことになるため、別Issueとして扱う。
#     現状ローカルでは、起動前に自分で環境変数へ入れる（README.md を参照）。
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
