"""scripts/dev.py の to_local_database_url() を検証するテスト。

守りたいこと:
    1. Secret Manager に入っている Cloud Run 用の接続URL（Unix ソケット指定）が、
       ローカルの cloud-sql-proxy 向け（localhost:5432）に変換されること。
       ここが壊れると、ローカルで起動してもDBに繋がらず開発が止まる。
    2. パスワードの記号が壊れないこと。urlsplit が返す値はパーセントエンコード
       されたままなので、素直に quote すると %40 が %2540 になり認証に失敗する。
       目視では気づきにくいので、テストで固定する。
    3. 形式が想定と違うときはエラーで止まり、そのメッセージに接続URLの中身
       （パスワード）を含めないこと。エラーメッセージは端末やCIのログに残るため。

このテストは Secret Manager にはアクセスしない。すべてダミー値で検証する。

import について:
    scripts/ は app/ の外にあるが、__init__.py が無くても名前空間パッケージとして
    import できる（Python 3.3+）。pytest は tests/ に __init__.py があるため
    プロジェクトルートを sys.path に入れる。よって sys.path への追加は不要。
"""

import pytest

from scripts.dev import to_local_database_url

# Cloud Run 用の接続URL（ダミー値）。Unix ソケットを ?host= で指定する形式。
_CLOUD_RUN_URL = "postgresql://user:pass@/founder?host=/cloudsql/proj:region:inst"

# エラーメッセージに漏れていないことを確かめるための、目印になるダミーパスワード。
_SECRET_PASSWORD = "dummy-secret-password"


def test_cloud_run_url_becomes_localhost_url() -> None:
    """Cloud Run 形式のURLが、localhost:5432 形式に変換されることを確かめる。"""
    assert to_local_database_url(_CLOUD_RUN_URL) == "postgresql://user:pass@localhost:5432/founder"


def test_socket_query_is_removed() -> None:
    """Unix ソケット指定（?host=/cloudsql/...）が変換後に残っていないことを確かめる。"""
    result = to_local_database_url(_CLOUD_RUN_URL)

    assert "cloudsql" not in result
    assert "?" not in result


def test_encoded_password_is_not_double_escaped() -> None:
    """エンコード済みのパスワードが、二重エスケープされずそのまま保たれることを確かめる。

    p%40ss%3Aword は p@ss:word をエンコードした値。ここが %2540 になると
    パスワードが変わってしまい、DBの認証に失敗する。
    """
    url = "postgresql://user:p%40ss%3Aword@/founder?host=/cloudsql/proj:region:inst"

    assert to_local_database_url(url) == "postgresql://user:p%40ss%3Aword@localhost:5432/founder"


def test_url_without_password_is_converted() -> None:
    """パスワードが無いURLでも、ユーザー名を保ったまま変換できることを確かめる。"""
    url = "postgresql://user@/founder?host=/cloudsql/proj:region:inst"

    assert to_local_database_url(url) == "postgresql://user@localhost:5432/founder"


def test_url_without_username_raises() -> None:
    """ユーザー名が無いURLは、変換せず RuntimeError で止まることを確かめる。"""
    url = f"postgresql://:{_SECRET_PASSWORD}@/founder?host=/cloudsql/proj:region:inst"

    with pytest.raises(RuntimeError):
        to_local_database_url(url)


def test_url_without_database_name_raises() -> None:
    """データベース名が無いURLは、変換せず RuntimeError で止まることを確かめる。"""
    url = f"postgresql://user:{_SECRET_PASSWORD}@/?host=/cloudsql/proj:region:inst"

    with pytest.raises(RuntimeError):
        to_local_database_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://:{_SECRET_PASSWORD}@/founder?host=/cloudsql/proj:region:inst",
        f"postgresql://user:{_SECRET_PASSWORD}@/?host=/cloudsql/proj:region:inst",
    ],
)
def test_error_message_does_not_leak_url(url: str) -> None:
    """エラーメッセージに、渡したURLの中身（パスワード）が含まれないことを確かめる。"""
    with pytest.raises(RuntimeError) as exc_info:
        to_local_database_url(url)

    message = str(exc_info.value)

    assert _SECRET_PASSWORD not in message
    assert url not in message
