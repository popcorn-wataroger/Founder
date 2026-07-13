"""_sanitize_for_log（ログインジェクション対策）のテスト。

何を守りたいか:
    ログは「1行 = 1レコード」として読まれる。値に改行が混ざっていると、
    攻撃者が偽のログ行を丸ごと差し込め、調査や監査を欺ける。
    そこで、ログに渡す前に改行を落として値を1行に閉じ込める。
"""

import pytest

from app.routers.sources_router import _sanitize_for_log


@pytest.mark.parametrize(
    ("入力", "期待"),
    [
        # \n（LF）で偽のログ行を差し込もうとするケース
        ("1\nINFO: 偽ログ", "1INFO: 偽ログ"),
        # \r（CR）だけでも、端末やビューアによっては行頭に戻って表示を偽装できる
        ("1\rINFO: 偽ログ", "1INFO: 偽ログ"),
        # \r\n（Windows形式の改行）
        ("1\r\nINFO: 偽ログ", "1INFO: 偽ログ"),
        # 複数行を差し込もうとするケース
        ("1\nINFO: 偽ログ1\nINFO: 偽ログ2", "1INFO: 偽ログ1INFO: 偽ログ2"),
    ],
)
def test_改行を含む値は改行が除去される(入力: str, 期待: str) -> None:
    result = _sanitize_for_log(入力)
    assert result == 期待
    # 念のため、戻り値に改行が1つも残っていないことを直接確かめる
    assert "\n" not in result
    assert "\r" not in result


@pytest.mark.parametrize(
    ("入力", "期待"),
    [
        (5, "5"),  # int（delete_source の source_id は現状これ）
        ("5", "5"),
        ("source-abc", "source-abc"),
        ("", ""),
        (None, "None"),
    ],
)
def test_改行を含まない値はそのまま文字列化される(入力: object, 期待: str) -> None:
    """無害な値は加工せず、ただ文字列にするだけ（ログの読みやすさを損なわない）。"""
    assert _sanitize_for_log(入力) == 期待
