"""アップロード済みファイルの「安全な保存先の名前」を組み立て直す専用モジュール。

なぜ独立したモジュールにするか:
    DBの file_path から実体を読む処理は複数箇所にある（本文抽出・ダウンロード・削除）。
    同じ安全ルールを各所に書き写すと、保存名の形式を変えたときに追従漏れが起き、
    片方だけ穴が開く。判定ルールを1箇所に集約して「唯一の正解」にする。

このモジュールの基本方針（パストラバーサル対策）:
    「検査だけして元の値を使う」形は取らない。
    検証を通った要素から新しい値を作り直して sink（open や GCSのSDK）に渡す。
    こうすると静的解析(CodeQL)から見ても、入力の汚染が sink まで届かなくなる。

保存先が2種類あることについて:
    ローカルの uploads/ とGCSのバケットで保存先が分かれるが、
    「名前を検証する」部分（sanitize_upload_name）は完全に共通で、
    そこから何を組み立てるかだけが違う。
        build_safe_upload_path … ローカル用。UPLOAD_DIR 配下のパスを作る
        build_safe_object_name … GCS用。定数プレフィックス付きのオブジェクト名を作る
    検証を1つにしておけば、保存名の規則を変えるときも直す場所は1箇所で済む。
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

# GCSのオブジェクト名の先頭に必ず付ける、コード側が持つ定数のプレフィックス。
# バケット直下に散らかさず「アップロード由来のファイル置き場」をまとめる目的と、
# オブジェクト名の材料を定数側に寄せる目的を兼ねる。
GCS_OBJECT_PREFIX = "sources/"


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

        「検査だけして元の値を使う」形は取らない。SSRF対策と同じく、検証を通った要素から
        新しい値を作り直して sink に渡すことで、静的解析(CodeQL)から見ても
        入力の汚染が sink まで届かなくなる。

        そのうえで最後に resolve() + is_relative_to() の確認も行う（多層防御）。
        こちらは入力を通すための検査ではなく、将来この関数の組み立て方が変わったときに
        黙って穴が開くのを防ぐための保険。
    """
    safe_name = sanitize_upload_name(raw_path)
    safe_path = UPLOAD_DIR / safe_name

    # 組み立てた結果が本当に UPLOAD_DIR の中か、最後にもう一度確かめる（多層防御）。
    # '..' やシンボリックリンクを解決した実体パスで比べる
    #
    # CodeQL は raw_path 由来の値が resolve() に渡ったと見なすが、この行は
    # 攻撃者の入力をパスに変える処理ではなく、むしろ**その逆の検査**にあたる。誤検知として抑制する。
    #
    # ここへ到達する時点で safe_path が安全な理由:
    #     1つ上の行の safe_name は sanitize_upload_name を通っている。
    #     同関数は Path(raw_path).name でディレクトリ部分（'../' や 'uploads/' や
    #     Windows形式の区切り）を全て捨てたうえで、SAFE_FILE_NAME_PATTERN
    #     （20桁タイムスタンプ + uuid4 + pdf/docx/pptx/txt）に fullmatch しない値を
    #     ValueError で弾く。safe_path はその戻り値と定数 UPLOAD_DIR だけから
    #     組み立てており、入力のディレクトリ部分は材料に入っていない。
    #     つまり raw_path の汚染は、この行に届く前に断ち切られている。
    #
    # この行自体の役目:
    #     将来 build_safe_upload_path の組み立て方が変わったときに、黙って穴が開くのを
    #     防ぐための保険。通す側の検査ではなく、落とす側の検査なので抑制しても
    #     防御が減ることはない。
    #
    # 検証: tests/test_storage.py の危険な保存パス8種 × 5操作のテストで、
    #       UPLOAD_DIR の外にあるおとりファイルを読むことも消すこともできないことを確認済み。
    #
    # 抑制の構文はアラート行の「直前の独立した行」に置く必要がある（行末に置くと効かない）
    # codeql[py/path-injection]
    if not safe_path.resolve().is_relative_to(UPLOAD_DIR.resolve()):
        raise ValueError("unexpected file path")

    return safe_path


def build_safe_object_name(raw_path: str) -> str:
    """検証済みのファイル名と定数プレフィックスだけから、GCSのオブジェクト名を組み立てて返す。

    入力:
        raw_path … DBに記録された保存パス文字列（信用しない値として扱う）

    出力:
        GCSのオブジェクト名（例: 'sources/20260101000000000000_<uuid>.pdf'）

    例外:
        ValueError … 名前が保存時の生成規則に一致しない場合

    処理（バケット外を指させないための対策）:
        build_safe_upload_path と同じ考え方で、材料を
        「コード側の定数 GCS_OBJECT_PREFIX」と「正規表現を通ったファイル名」の
        2つだけに限定する。入力のディレクトリ部分は sanitize_upload_name の
        Path(...).name で全て捨てられるため、'../' や '/' を含む名前や
        'gs://別のバケット/...' のような値は、そもそもここを通り抜けられない。

    補足（バケットはなぜ混ざらないか）:
        GCSでは「どのバケットか」はオブジェクト名ではなく、SDKに渡すバケット指定で決まる。
        app/storage.py は常に定数（config の GCS_BUCKET_NAME）からバケットを取得するため、
        オブジェクト名に何が入っても別のバケットへは届かない。
        この関数の検証と合わせて二重の防御になっている。
    """
    safe_name = sanitize_upload_name(raw_path)
    return f"{GCS_OBJECT_PREFIX}{safe_name}"
