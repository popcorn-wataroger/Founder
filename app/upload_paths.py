"""アップロード済みファイルの「安全な保存パス」を組み立て直す専用モジュール。

なぜ独立したモジュールにするか:
    DBの file_path から実ファイルを開く処理は複数箇所にある（本文抽出・ダウンロード）。
    同じ安全ルールを各所に書き写すと、保存名の形式を変えたときに追従漏れが起き、
    片方だけ穴が開く。判定ルールを1箇所に集約して「唯一の正解」にする。

このモジュールの基本方針（パストラバーサル対策）:
    「検査だけして元の値を使う」形は取らない。
    検証を通った要素から新しい値を作り直して sink（open や FileResponse）に渡す。
    こうすると静的解析(CodeQL)から見ても、入力の汚染が sink まで届かなくなる。
"""

import re
from pathlib import Path

from app.config import UPLOAD_DIR

# アップロード時にサーバー側が組み立てる保存ファイル名の形式。
# sources_router の save_name = f"{timestamp}_{uuid4}{拡張子}" に対応する
#   timestamp … %Y%m%d%H%M%S%f の20桁
#   uuid4     … 8-4-4-4-12 のハイフン付き36文字
SAFE_FILE_NAME_PATTERN = re.compile(
    r"[0-9]{20}_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.(?:pdf|docx|pptx|txt)"
)


def sanitize_upload_name(raw_path: str) -> str:
    """保存パス文字列からファイル名だけを取り出し、想定どおりの形式か検証して返す。

    入力:
        raw_path … DBに記録された保存パス文字列（信用しない値として扱う）

    出力:
        検証を通ったファイル名（ディレクトリ部分を含まない文字列）

    例外:
        ValueError … 名前が保存時の生成規則に一致しない場合

    補足:
        Path(...).name は末尾の要素だけを取り出すため、この時点で
        'uploads/' や '../' といったディレクトリ部分は全て捨てられる。
        そのうえで正規表現に完全一致するかを確かめるので、区切り文字も '..' も
        通り抜けられない。
    """
    name = Path(raw_path).name

    # 保存時にサーバー側が組み立てた名前の形式と一致しなければ拒否する
    if not SAFE_FILE_NAME_PATTERN.fullmatch(name):
        raise ValueError("unexpected file name")

    return name


def build_safe_upload_path(raw_path: str) -> Path:
    """検証済みのファイル名と定数ディレクトリだけから、安全なパスを組み立て直して返す。

    入力:
        raw_path … DBに記録された保存パス文字列（信用しない値として扱う）

    出力:
        UPLOAD_DIR 配下を指すパス（Path）

    例外:
        ValueError … 名前が保存時の生成規則に一致しない場合

    処理（パストラバーサル対策）:
        パスの材料を「コード側の定数 UPLOAD_DIR」と「正規表現を通ったファイル名」の
        2つだけに限定する。入力パスのディレクトリ部分は一切使わないため、
        UPLOAD_DIR の外を指すパスは原理的に組み立てられない。

        resolve() や is_relative_to() による「検査だけして元の値を使う」形は取らない。
        SSRF対策と同じく、検証を通った要素から新しい値を作り直して sink に渡すことで、
        静的解析(CodeQL)から見ても入力の汚染が sink まで届かなくなる。
    """
    safe_name = sanitize_upload_name(raw_path)
    return UPLOAD_DIR / safe_name
