"""保存名を組み立てる build_save_name（app/upload_paths.py）のテスト。

何を守りたいか:
    1. 作った保存名が、そのまま検証を通ること
       （生成側 build_save_name と検証側 SAFE_FILE_NAME_PATTERN がズレていないこと）
    2. 同時アップロードでも保存名が衝突しないこと
    3. 想定外の file_type を渡したとき、あいまいな名前を作らずに止まること

なぜ 1 をテストするか:
    この2つがズレると、アップロードは成功するのにダウンロードと削除だけが
    404 になる。画面上はエラーが出ないため、気づくのが非常に遅れる。
    「作った名前は必ず検証を通る」をテストで固定しておけば、
    保存名の形式を触ったときにその場で落ちる。
"""

import pytest

from app.upload_paths import EXTENSION_BY_TYPE, SAFE_FILE_NAME_PATTERN, build_save_name


@pytest.mark.parametrize("file_type", ["pdf", "docx", "pptx", "txt"])
def test_作った保存名は検証を通る(file_type: str) -> None:
    """4形式すべてで、生成した名前が保存名の生成規則に完全一致する。

    fullmatch を使うのは、実際に検証している sanitize_upload_name が
    fullmatch で判定しているため。前方一致では、末尾に余計な文字が付いた名前を
    見逃してしまう。
    """
    save_name = build_save_name(file_type)

    assert SAFE_FILE_NAME_PATTERN.fullmatch(save_name)

    # 拡張子は、ユーザー入力ではなくコード側の定数から付いていること
    assert save_name.endswith(EXTENSION_BY_TYPE[file_type])


def test_連続して呼んでも保存名が衝突しない() -> None:
    """同じ形式で続けて呼んでも、すべて別の名前になる。

    保存名が重複すると、後から保存したファイルが前のファイルを上書きしてしまう。
    タイムスタンプはミリ秒より細かくても同じ値になりうるため、
    衝突を防いでいるのは uuid4 の部分。
    """
    save_names = [build_save_name("pdf") for _ in range(100)]

    assert len(set(save_names)) == 100


@pytest.mark.parametrize("file_type", ["url", "exe", "PDF", "", "pdf.txt"])
def test_未対応の種別はValueErrorで止まる(file_type: str) -> None:
    """対応表に無い種別は、拡張子を推測して代用せずに例外にする。

    'PDF'（大文字）も対応表に無いので弾かれる。
    呼び出し元の upload_source は suffix を小文字にしてから渡しているので、
    通常はここに到達しない。
    """
    with pytest.raises(ValueError):
        build_save_name(file_type)
