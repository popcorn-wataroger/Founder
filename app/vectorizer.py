"""ソースの本文抽出・ベクトル化を担う司令塔モジュール。

この段階では「① 本文抽出」のみ実装している。
embedding（ベクトル化）や Qdrant への保存は後続ステップで追加する。
"""

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from docx import Document
from google import genai
from pptx import Presentation
from pypdf import PdfReader

from app.config import GEMINI_API_KEY, UPLOAD_DIR

# URL取得時のタイムアウト秒数（応答が無いURLで固まらないための保険）
URL_TIMEOUT = 10

# アップロード時にサーバー側が組み立てる保存ファイル名の形式。
# sources_router の save_name = f"{timestamp}_{uuid4}{拡張子}" に対応する
#   timestamp … %Y%m%d%H%M%S%f の20桁
#   uuid4     … 8-4-4-4-12 のハイフン付き36文字
_SAFE_FILE_NAME_PATTERN = re.compile(
    r"[0-9]{20}_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.(?:pdf|docx|pptx|txt)"
)

# チャンク分割の設定（Issue #19 の完了条件）
CHUNK_SIZE = 500  # 1チャンクの最大文字数
CHUNK_OVERLAP = 100  # 隣り合うチャンク間で重ねる文字数

# embedding（ベクトル化）に使う Gemini のモデル名
# 後で「どのモデルを使ったか」を確認できるよう定数として持たせる
EMBEDDING_MODEL = "gemini-embedding-001"

# Gemini APIクライアント。APIキーは config 経由で読み込む（コードに直書きしない）
_client = genai.Client(api_key=GEMINI_API_KEY)


def extract_text(path: str, file_type: str) -> str:
    """ソースから本文テキストを取り出す。

    入力:
        path      … ファイルの保存パス。ただし file_type が 'url' の場合はURL文字列。
        file_type … ソースの種別（'pdf' / 'docx' / 'pptx' / 'txt' / 'url'）

    出力:
        本文テキスト（文字列）

    file_type ごとに専用の抽出処理へ振り分ける（ディスパッチ）。
    未対応の形式が来たら ValueError を投げる。
    """
    if file_type == "pdf":
        return _extract_pdf(path)
    if file_type == "docx":
        return _extract_docx(path)
    if file_type == "pptx":
        return _extract_pptx(path)
    if file_type == "txt":
        return _extract_txt(path)
    if file_type == "url":
        return _extract_url(path)
    raise ValueError(f"未対応のファイル形式です: {file_type}")


def _sanitize_upload_name(raw_path: str) -> str:
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
    if not _SAFE_FILE_NAME_PATTERN.fullmatch(name):
        raise ValueError("unexpected file name")

    return name


def _build_safe_upload_path(raw_path: str) -> Path:
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
    safe_name = _sanitize_upload_name(raw_path)
    return UPLOAD_DIR / safe_name


def _extract_pdf(path: str) -> str:
    """PDFから本文を抽出する。"""
    # 入力パスをそのまま渡さず、検証済みの要素から作り直した安全なパスだけを渡す
    reader = PdfReader(_build_safe_upload_path(path))
    # ページごとにテキストを取り出し、改行で連結する
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _extract_docx(path: str) -> str:
    """Word(.docx)から本文を抽出する。"""
    # 入力パスをそのまま渡さず、検証済みの要素から作り直した安全なパスだけを渡す
    document = Document(_build_safe_upload_path(path))
    # 段落（paragraph）ごとの文字列を改行で連結する
    paragraphs = [para.text for para in document.paragraphs]
    return "\n".join(paragraphs)


def _extract_pptx(path: str) -> str:
    """PowerPoint(.pptx)から本文を抽出する。"""
    # 入力パスをそのまま渡さず、検証済みの要素から作り直した安全なパスだけを渡す
    presentation = Presentation(_build_safe_upload_path(path))
    texts: list[str] = []
    # スライド → 図形(shape) の順にたどり、テキストを持つ図形だけ集める
    for slide in presentation.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
    return "\n".join(texts)


def _extract_txt(path: str) -> str:
    """テキスト(.txt)をそのまま読み込む。"""
    # 入力パスをそのまま開かず、検証済みの要素から作り直した安全なパスだけを開く
    safe_path = _build_safe_upload_path(path)

    # 文字化けを避けるため UTF-8 で読み込む
    with open(safe_path, encoding="utf-8") as f:
        return f.read()


def _resolved_ips_are_public(hostname: str) -> bool:
    """ホスト名を名前解決し、解決先IPがすべて公開IPかどうかを返す。

    入力:
        hostname … 検査したいホスト名

    出力:
        True  … 名前解決でき、解決先IPがすべて公開IPだった場合
        False … 名前解決に失敗、またはIPを1つでも内部向けが含まれる場合

    補足:
        「URL全体の安全判定」はあえてここに置かない。SSRFの sink（requests.get）
        の直前で url を条件に弾く必要があるため、URLに対する検査と通信は
        呼び出し側（_extract_url）の同一スコープにまとめてある。
        この関数は「名前解決したIP群が公開か」という副次的な判定だけを担い、
        DNS を差し替えてテストしやすくするために切り出している。
    """
    try:
        # ホスト名をIPアドレスに解決する。1つのホスト名が複数IPを持つことがあるので
        # 「全部」取り出し、1つでも危険なIPがあれば拒否する
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # 名前解決できないURLは安全か判断できない → 安全側に倒して拒否する
        return False

    for info in addr_infos:
        # sockaddr の先頭要素がIPアドレス文字列（例: '93.184.216.34'）
        # 型チェッカーからは str か int か判別できないため str() で明示的に文字列化する
        ip_str = str(info[4][0])
        # IPv6のリンクローカルは 'fe80::1%en0' のようにゾーンIDが付くので切り落とす
        ip_str = ip_str.split("%")[0]

        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            # IPとして解釈できない＝想定外。安全側に倒して拒否する
            return False

        # 内部向けのアドレスをまとめて弾く
        # is_private     … 社内LAN等（10.x / 172.16-31.x / 192.168.x）
        # is_loopback    … 自分自身（127.0.0.1）
        # is_link_local  … 169.254.x（クラウドのメタデータサーバーを含む）
        # is_reserved    … 予約済みアドレス
        # is_multicast   … マルチキャスト
        # is_unspecified … 0.0.0.0
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False

    # すべて公開IPだった
    return True


def _build_safe_public_url(raw_url: str) -> str:
    """入力URLを検証し、検証を通った要素だけから安全なURLを組み立て直して返す。

    入力:
        raw_url … 社長がフォームに入力した生のURL（信用できない値）

    出力:
        検証済みの要素だけで作り直したURL文字列

    例外:
        ValueError … スキーム/ホスト/ポート/認証情報のいずれかが不正な場合

    補足:
        生の raw_url をそのまま通信に渡さないのが要点。urlsplit で分解し、
        個々の要素を検査したうえで urlunsplit で新しい文字列を作る。
        これにより「汚染された入力そのもの」が sink（requests.get）へ届かず、
        静的解析(CodeQL)にも検証を通過した値であることが伝わる。
    """
    parsed = urlsplit(raw_url)

    # http / https 以外（file:// や gopher:// など）は最初から拒否する
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("unsupported scheme")

    host = parsed.hostname
    if not host:
        raise ValueError("missing host")

    # ホスト名の解決先が1つでも内部向けIPなら、通信せずに打ち切る
    if not _resolved_ips_are_public(host):
        raise ValueError("non-public host")

    # 不正なポート表記（例: http://example.com:abc/）は .port の参照時に例外になる
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid port") from exc

    # user:pass@host 形式は、意図しない認証情報の送信につながるため拒否する
    if parsed.username or parsed.password:
        raise ValueError("credentials are not allowed")

    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or "/"

    # 検証を通った要素だけで新しいURLを組み立てる
    safe_url = urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, parsed.fragment))
    return safe_url


def _extract_url(url: str) -> str:
    """URLのウェブページから本文を抽出する。

    入力:
        url … 取得したいウェブページのURL

    出力:
        ページの表示テキスト（文字列）

    処理（SSRF対策）:
        URLは社長がフォームから自由に入力できる。もし何の検査もせずに
        requests.get すると、サーバー自身に「内部だけに見えるアドレス」へ
        アクセスさせられてしまう（SSRF＝サーバー側リクエスト偽造）。
        たとえば http://169.254.169.254/ はクラウド(GCP等)のメタデータサーバーで、
        サービスアカウントのアクセストークンが取れてしまう。
        http://localhost:8000/ や社内ネットワークのIPも同様に危険。

        そこで _build_safe_public_url で検査済みの要素だけからURLを組み立て直し、
        入力された url そのものではなく、その safe_url だけを requests.get に渡す。
    """
    safe_url = _build_safe_public_url(url)

    # sink に到達するのは、検証済み要素から作り直した safe_url だけ。
    # timeout … 応答が無いURLで固まらないための保険
    # allow_redirects=False … 公開URLに見せかけて内部アドレスへ302転送する
    #                         抜け道を塞ぐため、リダイレクトは追わない
    response = requests.get(safe_url, timeout=URL_TIMEOUT, allow_redirects=False)
    response.raise_for_status()

    # HTMLを解析し、本文に不要なタグ（script/style）を取り除く
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    # 表示テキストだけを取り出す
    return soup.get_text(separator="\n", strip=True)


def split_into_chunks(text: str) -> list[str]:
    """本文テキストをチャンク（小さな塊）のリストに分割する。

    入力:
        text … 分割したい本文テキスト

    出力:
        チャンク文字列のリスト

    なぜ分割するか:
        長文をそのままベクトル化すると要点がぼやけ、検索精度が落ちる。
        500文字ごとの小さな塊に分けることで、質問に近い部分だけを探しやすくする。

    なぜ重ねる（オーバーラップ）か:
        区切り目で文が途中に切れると意味が失われる。
        前のチャンクの末尾100文字を次のチャンクの先頭に重ねることで、
        文脈が境界で途切れるのを防ぐ。
    """
    # 空文字なら分割対象がないので空リストを返す
    if not text:
        return []

    # 次のチャンク開始位置をどれだけ進めるか（500 - 100 = 400文字ずつ前進）
    step = CHUNK_SIZE - CHUNK_OVERLAP

    chunks: list[str] = []
    start = 0
    while start < len(text):
        # 現在位置から最大500文字を1チャンクとして切り出す
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        # 末尾まで到達したら終了（重複した余分なチャンクを作らない）
        if end >= len(text):
            break
        # オーバーラップ分を残して開始位置を前進させる
        start += step

    return chunks


def embed_text(text: str) -> list[float]:
    """1チャンクのテキストを数値ベクトル（embedding）に変換する。

    入力:
        text … ベクトル化したいテキスト（通常はチャンク1つ）

    出力:
        数値（float）のリスト＝ベクトル。
        gemini-embedding-001 は既定で3072次元のベクトルを返す。
        実際の次元数は len(戻り値) で後から確認できる。

    処理:
        Gemini の embedding 用モデルにテキストを渡し、
        「意味」を表す数値の並びを受け取る。
        この数値の近さ同士を比べることで、後段のRAGで
        質問に意味が近いチャンクを検索できるようになる。
    """
    # Gemini にテキストを送ってベクトルを生成してもらう
    result = _client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
    )
    # 戻り値は embedding のリスト。今回は1件なので先頭の数値列(values)を返す
    return result.embeddings[0].values
