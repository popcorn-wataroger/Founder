"""app/config.py の設定読み込みを検証するテスト。

守りたいこと:
    1. 本番相当の環境で JWT_SECRET_KEY が未設定なら、起動を続けずに失敗すること。
    2. 本番相当の環境で任意設定（Gemini / Qdrant）が未設定なら、変数名を警告に出すこと。

1 は漏洩の問題。固定のフォールバック値を置くと、その値を知っている人が管理者(ADMIN)の
トークンを自作でき、全社員の個別ソースを閲覧できてしまう。しかも起動は成功するため、
手動確認では「動いてしまう」ことに気づけない。
将来 config.py を触った人がフォールバックを復活させたら、ここで落とす。

2 は可用性の問題。起動は止めないぶん、警告が消えると本番の設定漏れに気づく手段が
無くなる。値そのものをログに出さないことも合わせて担保する。
"""

import contextlib
import importlib
import logging
import os
from collections.abc import Iterator

import dotenv
import pytest

import app.config

# テストごとに与え直す環境変数。持ち越すと前のケースの値が次に影響するため、
# 開始時に必ず消してから必要なものだけを設定する。
_MANAGED_ENV_NAMES = (
    "APP_ENV",
    "JWT_SECRET_KEY",
    "GEMINI_API_KEY",
    "QDRANT_URL",
    "QDRANT_API_KEY",
)


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


# 未設定警告のテスト用。JWT_SECRET_KEY を設定しないと本番相当では
# RuntimeError で落ちてしまい、警告まで到達できないため常に渡す。
_DUMMY_JWT_SECRET_KEY = "dummy-jwt-secret-for-test"

# 「値そのものがログに出ない」ことを確かめるための、設定済みのダミー値。
_DUMMY_GEMINI_API_KEY = "dummy-gemini-api-key-value"


def _reload_and_capture_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """app.config を読み込み直し、そのとき出た警告メッセージだけを取り出す。

    入力: caplog（pytest のログ捕捉フィクスチャ）
    処理: 直前までのログを捨ててから reload し、WARNING 以上の記録を集める
    出力: 警告メッセージの文字列リスト（警告が無ければ空リスト）
    """
    caplog.clear()
    importlib.reload(app.config)
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


def test_production_warns_missing_optional_settings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """本番相当で Gemini / Qdrant が未設定なら、変数名を挙げて警告する。

    起動は止めないので、この警告が消えると本番の設定漏れに気づけなくなる。
    """
    with _config_env(APP_ENV="production", JWT_SECRET_KEY=_DUMMY_JWT_SECRET_KEY):
        with caplog.at_level(logging.WARNING, logger="app.config"):
            warnings = _reload_and_capture_warnings(caplog)

            assert len(warnings) == 1
            assert "GEMINI_API_KEY" in warnings[0]
            assert "QDRANT_URL" in warnings[0]
            assert "QDRANT_API_KEY" in warnings[0]


def test_unset_app_env_warns_missing_optional_settings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """APP_ENV 未設定も本番相当として扱い、未設定の項目を警告する。"""
    with _config_env(JWT_SECRET_KEY=_DUMMY_JWT_SECRET_KEY):
        with caplog.at_level(logging.WARNING, logger="app.config"):
            warnings = _reload_and_capture_warnings(caplog)

            assert len(warnings) == 1
            assert "GEMINI_API_KEY" in warnings[0]


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_dev_envs_do_not_warn_missing_optional_settings(
    app_env: str, caplog: pytest.LogCaptureFixture
) -> None:
    """local / test では未設定でも警告しない。

    開発とCIでは Gemini も Qdrant も使わずに起動するのが普通なので、
    ここで警告を出すと毎回出続けて、本番の警告を見落とす原因になる。
    """
    with _config_env(APP_ENV=app_env):
        with caplog.at_level(logging.WARNING, logger="app.config"):
            warnings = _reload_and_capture_warnings(caplog)

            assert warnings == []


def test_production_with_all_optional_settings_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """本番相当でも、すべて設定済みなら警告しない。"""
    with _config_env(
        APP_ENV="production",
        JWT_SECRET_KEY=_DUMMY_JWT_SECRET_KEY,
        GEMINI_API_KEY=_DUMMY_GEMINI_API_KEY,
        QDRANT_URL="https://qdrant.example.com",
        QDRANT_API_KEY="dummy-qdrant-api-key-value",
    ):
        with caplog.at_level(logging.WARNING, logger="app.config"):
            warnings = _reload_and_capture_warnings(caplog)

            assert warnings == []


def test_warning_lists_only_missing_names_and_hides_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """警告には未設定の変数名だけが載り、設定済みの値は載らない。

    ログは開発者以外の目にも触れる場所に残るため、鍵の値が出ていないことを
    テストで固定しておく。
    """
    with _config_env(
        APP_ENV="production",
        JWT_SECRET_KEY=_DUMMY_JWT_SECRET_KEY,
        GEMINI_API_KEY=_DUMMY_GEMINI_API_KEY,
    ):
        with caplog.at_level(logging.WARNING, logger="app.config"):
            warnings = _reload_and_capture_warnings(caplog)

            assert len(warnings) == 1
            message = warnings[0]
            # 設定済みの GEMINI_API_KEY は未設定リストに載らない
            assert "GEMINI_API_KEY" not in message
            assert "QDRANT_URL" in message
            assert "QDRANT_API_KEY" in message
            # 値そのものは出さない
            assert _DUMMY_GEMINI_API_KEY not in message
            assert _DUMMY_JWT_SECRET_KEY not in message
