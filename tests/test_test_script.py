"""scripts/test.py の to_test_database_url() と ensure_test_database_url() を検証するテスト。

守りたいこと:
    1. 接続先のデータベース名が、必ずテスト用(founder_test)に差し替わること。
       差し替えが効かないと、開発用DBに対してテストが走る。
    2. ensure_test_database_url() が、テスト用DB以外を確実に止めること。
       この関数はデータ消失を防ぐ最後の関門で、tests/conftest.py はこの先で
       テーブルを TRUNCATE する。壊れたことに気づくのは消えた後になるため、
       「通してはいけないもの」を明示的に固定する。
    3. 差し替えでユーザー名・パスワード・接続先が壊れないこと。
       壊れると認証に失敗し、原因がURLのどこかも分かりにくい。
    4. エラーメッセージに接続URLの中身（パスワード）を含めないこと。
       エラーメッセージは端末やCIのログに残るため。

このテストは Secret Manager にはアクセスしない。すべてダミー値で検証する。
"""

import pytest

from scripts.test import ensure_test_database_url, to_test_database_url

# ローカル用の接続URL（ダミー値）。開発用データベース founder を指している。
_LOCAL_URL = "postgresql://user:pass@localhost:5432/founder"

# エラーメッセージに漏れていないことを確かめるための、目印になるダミーパスワード。
_SECRET_PASSWORD = "dummy-secret-password"


def test_database_name_is_replaced_with_test_database() -> None:
    """データベース名が founder_test に置き換わることを確かめる。"""
    assert to_test_database_url(_LOCAL_URL) == "postgresql://user:pass@localhost:5432/founder_test"


def test_already_test_database_stays_same() -> None:
    """既に founder_test を指しているURLは、変換しても同じ結果になることを確かめる。"""
    url = "postgresql://user:pass@localhost:5432/founder_test"

    assert to_test_database_url(url) == url


def test_credentials_and_host_are_kept() -> None:
    """ユーザー名・パスワード・ホスト・ポートが変わらないことを確かめる。

    変わるのはデータベース名だけ。ここが崩れるとDBに接続できなくなる。
    """
    result = to_test_database_url(_LOCAL_URL)

    assert result.startswith("postgresql://user:pass@localhost:5432/")


def test_encoded_password_is_kept() -> None:
    """エンコード済みのパスワードが、置き換えによって壊れないことを確かめる。"""
    url = "postgresql://user:p%40ss%3Aword@localhost:5432/founder"

    assert (
        to_test_database_url(url) == "postgresql://user:p%40ss%3Aword@localhost:5432/founder_test"
    )


def test_url_without_database_name_raises() -> None:
    """データベース名が無いURLは、置き換えず RuntimeError で止まることを確かめる。"""
    url = f"postgresql://user:{_SECRET_PASSWORD}@localhost:5432/"

    with pytest.raises(RuntimeError):
        to_test_database_url(url)


def test_ensure_accepts_test_database() -> None:
    """founder_test を指すURLは、そのまま通す（例外を出さない）ことを確かめる。"""
    ensure_test_database_url("postgresql://user:pass@localhost:5432/founder_test")


def test_ensure_rejects_development_database() -> None:
    """開発用の founder を指すURLは、RuntimeError で止めることを確かめる。

    ここを通すと、開発用DBのソース・チャット履歴・最終ログインが消える。
    """
    with pytest.raises(RuntimeError):
        ensure_test_database_url(_LOCAL_URL)


def test_ensure_rejects_similar_database_name() -> None:
    """founder_test で始まるだけの別名（founder_test_backup）も止めることを確かめる。

    前方一致で判定していると通ってしまうため、完全一致であることを固定する。
    """
    with pytest.raises(RuntimeError):
        ensure_test_database_url("postgresql://user:pass@localhost:5432/founder_test_backup")


@pytest.mark.parametrize(
    "database_name",
    ["founder", "founder_test_backup"],
)
def test_ensure_error_message_does_not_leak_url(database_name: str) -> None:
    """エラーメッセージに、渡したURLの中身（パスワード）が含まれないことを確かめる。"""
    url = f"postgresql://user:{_SECRET_PASSWORD}@localhost:5432/{database_name}"

    with pytest.raises(RuntimeError) as exc_info:
        ensure_test_database_url(url)

    message = str(exc_info.value)

    assert _SECRET_PASSWORD not in message
    assert url not in message
