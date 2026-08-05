"""app/config.py の JWT_SECRET_KEY 読み込みを検証するテスト。

守りたいこと:
    本番相当の環境で JWT_SECRET_KEY が未設定なら、起動を続けずに失敗すること。

固定のフォールバック値を置くと、その値を知っている人が管理者(ADMIN)のトークンを
自作でき、全社員の個別ソースを閲覧できてしまう。しかも起動は成功するため、
手動確認では「動いてしまう」ことに気づけない。
将来 config.py を触った人がフォールバックを復活させたら、ここで落とす。
"""

import contextlib
import importlib
import os
from collections.abc import Iterator

import dotenv
import pytest

import app.config

# テストごとに与え直す環境変数。持ち越すと前のケースの値が次に影響するため、
# 開始時に必ず消してから必要なものだけを設定する。
_MANAGED_ENV_NAMES = ("APP_ENV", "JWT_SECRET_KEY")


@contextlib.contextmanager
def _config_env(**env_vars: str) -> Iterator[None]:
    """指定した環境変数だけを与えて app.config を読み込み直せる状態にする。

    入力: 環境変数名と値（例: APP_ENV="production"）
    処理: 対象の環境変数を消して指定分だけを設定し、.env の読み込みを無効化する
    出力: なし（with の中で importlib.reload(app.config) を呼ぶ）

    load_dotenv を無効化するのは、実行者の手元にある .env の中身でテスト結果が
    変わらないようにするため。CIには .env が無いので、これを止めないと
    ローカルとCIで結果がずれる。

    with を抜けるときは環境変数を元に戻し、config も読み込み直すので、
    後続のテストは影響を受けない。
    """
    original_environ = dict(os.environ)
    original_load_dotenv = dotenv.load_dotenv

    for name in _MANAGED_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ.update(env_vars)
    dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]

    try:
        yield
    finally:
        dotenv.load_dotenv = original_load_dotenv  # type: ignore[assignment]
        os.environ.clear()
        os.environ.update(original_environ)
        importlib.reload(app.config)


def test_production_without_jwt_secret_key_fails() -> None:
    """APP_ENV=production かつ JWT_SECRET_KEY 未設定なら起動を止める。"""
    with _config_env(APP_ENV="production"):
        with pytest.raises(RuntimeError) as exc_info:
            importlib.reload(app.config)

    assert "JWT_SECRET_KEY" in str(exc_info.value)


def test_unset_app_env_is_treated_as_production() -> None:
    """APP_ENV 未設定も本番相当として扱い、JWT_SECRET_KEY 未設定なら起動を止める。

    本番で APP_ENV を渡し忘れたときに、開発用の既定値で起動が通ってしまわないこと。
    """
    with _config_env():
        with pytest.raises(RuntimeError) as exc_info:
            importlib.reload(app.config)

    assert "JWT_SECRET_KEY" in str(exc_info.value)


def test_local_without_jwt_secret_key_starts() -> None:
    """APP_ENV=local かつ JWT_SECRET_KEY 未設定なら開発用の鍵で起動できる。"""
    with _config_env(APP_ENV="local"):
        config = importlib.reload(app.config)

        assert config.JWT_SECRET_KEY
