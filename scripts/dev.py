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
import urllib.parse  # noqa: E402
from typing import Final  # noqa: E402

from google.api_core import exceptions as gcp_exceptions  # noqa: E402
from google.auth import exceptions as auth_exceptions  # noqa: E402
from google.cloud import secretmanager  # noqa: E402

PROJECT_ID: Final[str] = "notebooklm-482403"

# Secret Manager から取得して環境変数に入れる名前の一覧。
# シークレット名と環境変数名はあえて同じにしている（対応表を持たずに済むため）。
#
# DATABASE_URL をこの一覧に入れていない理由（Issue #82 で対応済み）:
#     取得はするが、この一覧の他の値と違って「そのまま環境変数へ入れる」ことが
#     できないため、別扱いにしている。Secret Manager に登録されているのは
#     Cloud Run 用の形式で、ローカルでは接続先の書き方が違うから。
#
#         登録値（Cloud Run）: postgresql://...@/founder?host=/cloudsql/<接続名>
#         ローカルで必要な値  : postgresql://...@localhost:5432/founder
#
#     Cloud Run は Unix ソケット経由で繋ぐが、ローカルは cloud-sql-proxy が
#     待ち受ける localhost へ繋ぐ。そこで to_local_database_url() で形式を
#     変換してから load_secrets_into_env() の中で環境変数に入れている。
SECRET_NAMES: Final[tuple[str, ...]] = (
    "JWT_SECRET_KEY",
    "GEMINI_API_KEY",
    "QDRANT_URL",
    "QDRANT_API_KEY",
)

# ローカルの cloud-sql-proxy が待ち受けるホストとポート。
LOCAL_DB_HOST: Final[str] = "localhost"
LOCAL_DB_PORT: Final[int] = 5432


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


def to_local_database_url(url: str) -> str:
    """Cloud Run 用の接続URLを、ローカル(cloud-sql-proxy)用の形式に変換する。

    入力: Cloud Run 用の接続URL
          例: postgresql://user:pass@/founder?host=/cloudsql/<接続名>
    処理: ユーザー名・パスワード・DB名だけを引き継ぎ、接続先を
          localhost:5432 に置き換える。Unix ソケットの指定(?host=...)は落とす。
    出力: ローカル用の接続URL
          例: postgresql://user:pass@localhost:5432/founder
    例外: 必要な要素（ユーザー名・DB名）が欠けている場合は RuntimeError

    エラーメッセージには URL の値そのものを含めない（パスワードが漏れるため）。
    """
    parts = urllib.parse.urlsplit(url)

    if not parts.username:
        raise RuntimeError(
            "DATABASE_URL にユーザー名が含まれていません。"
            "Secret Manager の DATABASE_URL の形式を確認してください。"
        )

    # path は "/founder" の形。先頭のスラッシュを除いた残りがDB名。
    if len(parts.path) <= 1:
        raise RuntimeError(
            "DATABASE_URL にデータベース名が含まれていません。"
            "Secret Manager の DATABASE_URL の形式を確認してください。"
        )

    # urlsplit が返す username / password はパーセントエンコードされたままなので、
    # 一度戻してから付け直す（そのまま quote すると %40 が %2540 になってしまう）。
    # quote の safe="" は「記号もすべてエスケープする」という指定。
    user = urllib.parse.quote(urllib.parse.unquote(parts.username), safe="")
    if parts.password:
        password = urllib.parse.quote(urllib.parse.unquote(parts.password), safe="")
        netloc = f"{user}:{password}@{LOCAL_DB_HOST}:{LOCAL_DB_PORT}"
    else:
        netloc = f"{user}@{LOCAL_DB_HOST}:{LOCAL_DB_PORT}"

    # 第4要素（クエリ）を空にすることで ?host=/cloudsql/... を落としている。
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def load_secrets_into_env() -> None:
    """機密情報を Secret Manager から取得し、環境変数として設定する。

    入力: なし（モジュール定数 PROJECT_ID / SECRET_NAMES を使う）
    処理: SECRET_NAMES の各値は、取得した値をそのまま os.environ に入れる。
          DATABASE_URL は Cloud Run 用の形式で登録されているため、取得後に
          to_local_database_url() でローカル用に変換してから os.environ に
          入れる。ただし既に DATABASE_URL が環境変数にある場合は、
          その指定を尊重して取得も設定もしない。
    出力: なし（副作用として os.environ が更新される）
    例外: 認証情報が無い場合は RuntimeError
          シークレットの取得に失敗した場合や、DATABASE_URL の形式が
          想定と違う場合も RuntimeError
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

    fetched_count = len(SECRET_NAMES)

    # DATABASE_URL は形式変換が必要なため、SECRET_NAMES とは別に扱う。
    # 既に環境変数へ入っている場合は上書きしない
    # （作業者が意図的に別のDBを指定しているときに、その指定を尊重するため）。
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = to_local_database_url(fetch_secret(client, "DATABASE_URL"))
        fetched_count += 1

    # 値そのものは絶対に出さない。出してよいのは「何件取得できたか」だけ。
    #
    # シークレット名を出力に含めない理由:
    # CodeQL(py/clear-text-logging-sensitive-data) はデータの流れを追う。
    # SECRET_NAMES は定数だが fetch_secret() に渡す値の供給元であるため、
    # 「機密に関係する値」として汚染マークが付く。名前を print すると、
    # 実際に出しているのが名前だけでも検出対象になる。
    # len() の結果は整数なので、文字列としての汚染は伝播しない。
    #
    # 動作確認には「何件取得できたか」が分かれば十分。
    # 途中で失敗した場合は、どのシークレットで止まったかを
    # fetch_secret() のエラーメッセージが持っている。
    print(f"{fetched_count} 件のシークレットを取得しました")


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
