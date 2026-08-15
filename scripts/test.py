"""ローカルでテストを実行するための起動スクリプト。

pytest は接続先(DATABASE_URL)が無いと実行できず、tests/conftest.py は
テストのたびにテーブルを TRUNCATE する。接続先を founder（開発用）に
向けたまま実行すると、ソース・チャット履歴・最終ログインが全部消える。

そこでこのスクリプトが、
  1. 接続情報を Secret Manager から取得してテスト用DBを指す形に組み立て、
  2. 本当にテスト用DB(founder_test)を指しているか確かめてから、
  3. pytest を起動する。
という順番を毎回同じように実行する。長い環境変数付きコマンドを手打ちしなくて済み、
DB名の変え忘れも起こらない。

機密情報以外の環境変数は .github/workflows/ci.yml の test ジョブと同じ
ダミー値に合わせている（実際のAPIキーは使わない）。CIと手元で同じ条件にすることで、
「手元では通るがCIで落ちる」を減らす。

前提（初回だけ）:
  1. gcloud auth application-default login
  2. cloud-sql-proxy を起動しておく（localhost:5432 で待ち受ける）
  3. テスト用データベース founder_test を作成しておく

使い方:
  uv run python scripts/test.py
  uv run python scripts/test.py -k dev_script   # 引数はそのまま pytest に渡る
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
from pathlib import Path  # noqa: E402
from typing import Final  # noqa: E402

# `uv run python scripts/test.py` として実行すると、sys.path の先頭は scripts/ になり、
# プロジェクトルートが入らないため scripts.dev を import できない。
# ルートを追加して、テストから import されたとき（既にルートが sys.path にある）と
# 同じ書き方で dev.py を再利用できるようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.auth import exceptions as auth_exceptions  # noqa: E402
from google.cloud import secretmanager  # noqa: E402

from scripts.dev import fetch_secret, to_local_database_url  # noqa: E402

# テスト専用のデータベース名。この名前以外を指していたら pytest を起動しない。
TEST_DB_NAME: Final[str] = "founder_test"

# テスト実行時に設定する、機密でない環境変数。
# .github/workflows/ci.yml の test ジョブと同じ値にそろえている
# （値を変えるときは ci.yml も一緒に変える）。
#
# GEMINI_API_KEY がダミーでよい理由: app/vectorizer.py が import 時に
# genai.Client を生成するため空だと収集前に落ちるが、テストは実際のAPIを呼ばない。
# JWT_SECRET_KEY を取得しない理由: APP_ENV=test は app/config.py が
# 開発用の既定値を使う環境なので、本物の署名鍵は要らない。
TEST_ENV: Final[dict[str, str]] = {
    "APP_ENV": "test",
    "GEMINI_API_KEY": "dummy-key-for-ci",
    "QDRANT_URL": "http://localhost:6333",
    "FRONTEND_URL": "http://localhost:8000",
}


def to_test_database_url(url: str) -> str:
    """接続URLのデータベース名を、テスト用(founder_test)に置き換える。

    入力: ローカル用の接続URL
          例: postgresql://user:pass@localhost:5432/founder
    処理: ユーザー名・パスワード・接続先ホストはそのまま引き継ぎ、
          データベース名の部分だけを TEST_DB_NAME に差し替える。
    出力: テスト用DBを指す接続URL
          例: postgresql://user:pass@localhost:5432/founder_test
    例外: データベース名が含まれていない接続URLの場合は RuntimeError

    エラーメッセージには URL の値そのものを含めない（パスワードが漏れるため）。
    """
    parts = urllib.parse.urlsplit(url)

    # path は "/founder" の形。先頭のスラッシュを除いた残りがDB名。
    if len(parts.path) <= 1:
        raise RuntimeError(
            "接続URLにデータベース名が含まれていないため、"
            f"テスト用の '{TEST_DB_NAME}' に置き換えられません。"
            "Secret Manager の DATABASE_URL の形式を確認してください。"
        )

    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, f"/{TEST_DB_NAME}", "", ""))


def ensure_test_database_url(url: str) -> None:
    """接続先がテスト用データベースであることを確かめる。

    入力: これから DATABASE_URL に入れる接続URL
    処理: データベース名が TEST_DB_NAME と一致するかを調べる
    出力: なし（一致していれば何も起きない）
    例外: 一致しない場合は RuntimeError（pytest を起動させない）

    なぜ組み立てた直後にもう一度確かめるか:
        tests/conftest.py はテストのたびに接続先のテーブルを TRUNCATE する。
        開発用DBを指したまま実行すると、ソース・チャット履歴・最終ログインが
        1件残らず消える。組み立て処理の書き換えやURL形式の想定外によって
        差し替えが効かなかった場合に、消す前の最後の関門としてここで止める。

    エラーメッセージには実際の接続先を含めない（パスワードを含むURLの一部のため）。
    """
    database_name = urllib.parse.urlsplit(url).path.lstrip("/")

    if database_name != TEST_DB_NAME:
        raise RuntimeError(
            f"接続先がテスト用データベース '{TEST_DB_NAME}' ではないため、"
            "テストを実行せずに中断しました。"
            "テストはテーブルを空にするため、開発用データベースに対しては実行できません。"
        )


def load_test_env() -> None:
    """テスト実行に必要な環境変数を設定する。

    入力: なし（モジュール定数 TEST_DB_NAME / TEST_ENV を使う）
    処理: Secret Manager から DATABASE_URL を取得し、ローカル用の形式に変換して
          データベース名をテスト用に置き換える。テスト用DBを指していることを
          確かめてから os.environ に入れ、続けて TEST_ENV の各値も設定する。
    出力: なし（副作用として os.environ が更新される）
    例外: 認証情報が無い場合や、取得・変換・検証に失敗した場合は RuntimeError

    DATABASE_URL が既に環境変数にあっても、ここで必ず上書きする。
    以前の作業で開発用DBを指す値が残っていると、テストがそれを消してしまうため。
    """
    try:
        client = secretmanager.SecretManagerServiceClient()
    except auth_exceptions.DefaultCredentialsError as exc:
        raise RuntimeError(
            "GCP の認証情報が見つかりません。"
            "'gcloud auth application-default login' を実行してください。"
        ) from exc

    database_url = to_test_database_url(to_local_database_url(fetch_secret(client, "DATABASE_URL")))

    # 環境変数に入れる前に確かめる（入れてしまってから気づいても遅い）
    ensure_test_database_url(database_url)

    os.environ["DATABASE_URL"] = database_url
    for name, value in TEST_ENV.items():
        os.environ[name] = value

    # 接続URLそのものは出さない。出してよいのは「どのDBを使うか」だけ。
    print(f"テスト用の環境変数を設定しました（接続先: {TEST_DB_NAME}）")


def main() -> int:
    """環境変数を整えてから pytest を起動する。

    入力: コマンドライン引数（そのまま pytest に渡す）
    処理: load_test_env() で環境変数を設定し、pytest にプロセスを差し替える
    出力: 終了コード（失敗時のみ 1 を返す。成功時は pytest に置き換わるため返らない）
    """
    try:
        load_test_env()
    except RuntimeError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    # 現在のプロセスを pytest に置き換える（Ctrl+C がそのまま pytest に届く）
    os.execvp("uv", ["uv", "run", "pytest", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
