"""ソースの本文抽出・ベクトル化を担う司令塔モジュール。

この段階では「① 本文抽出」のみ実装している。
embedding（ベクトル化）や Qdrant への保存は後続ステップで追加する。
"""

import ipaddress
import socket
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from docx import Document
from google import genai
from pptx import Presentation
from pypdf import PdfReader

from app.config import GEMINI_API_KEY

# URL取得時のタイムアウト秒数（応答が無いURLで固まらないための保険）
URL_TIMEOUT = 10

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


def _extract_pdf(path: str) -> str:
    """PDFから本文を抽出する。"""
    reader = PdfReader(path)
    # ページごとにテキストを取り出し、改行で連結する
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _extract_docx(path: str) -> str:
    """Word(.docx)から本文を抽出する。"""
    document = Document(path)
    # 段落（paragraph）ごとの文字列を改行で連結する
    paragraphs = [para.text for para in document.paragraphs]
    return "\n".join(paragraphs)


def _extract_pptx(path: str) -> str:
    """PowerPoint(.pptx)から本文を抽出する。"""
    presentation = Presentation(path)
    texts: list[str] = []
    # スライド → 図形(shape) の順にたどり、テキストを持つ図形だけ集める
    for slide in presentation.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
    return "\n".join(texts)


def _extract_txt(path: str) -> str:
    """テキスト(.txt)をそのまま読み込む。"""
    # 文字化けを避けるため UTF-8 で読み込む
    with open(path, encoding="utf-8") as f:
        return f.read()


def _ensure_safe_url(url: str) -> str:
    """URLが「外部の公開サーバー」を指しているかを検査し、安全なURLだけを返す。

    入力:
        url … 検査したいURL文字列

    出力:
        安全だと確認できた url をそのまま返す
        （http/https で、名前解決したIPがすべて公開IPだった場合のみ）

    例外:
        ValueError … スキーム違反・名前解決失敗・内部IPを含むなど、
                     安全と確認できなかった場合

    なぜ「値を返す」形なのか（SSRF対策）:
        URLは社長がフォームから自由に入力できる。もし何の検査もせずに
        requests.get すると、サーバー自身に「内部だけに見えるアドレス」へ
        アクセスさせられてしまう。これをSSRF（サーバー側リクエスト偽造）と呼ぶ。
        たとえば http://169.254.169.254/ はクラウド(GCP等)のメタデータサーバーで、
        サービスアカウントのアクセストークンが取れてしまう。
        http://localhost:8000/ や社内ネットワークのIPも同様に危険。
        そこで「公開インターネット上のIPだけ許可する」という関門をここに置く。

        bool を返すだけの判定関数だと「検査した値」と「実際に requests.get へ
        渡す値」が別物になり、静的解析(CodeQL)からはガードが素通しに見える。
        検査を通った url 自身を返し、その戻り値を sink に渡すことで、
        「検証 → 使用」が一本のデータフロー上に乗り、関門として機能する。
    """
    parsed = urlparse(url)

    # http / https 以外（file:// や gopher:// など）は最初から拒否する
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"このURLは取得できません: {url}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"このURLは取得できません: {url}")

    try:
        # ホスト名をIPアドレスに解決する。1つのホスト名が複数IPを持つことがあるので
        # 「全部」取り出し、1つでも危険なIPがあれば拒否する
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        # 名前解決できないURLは安全か判断できない → 安全側に倒して拒否する
        raise ValueError(f"このURLは取得できません: {url}") from exc

    for info in addr_infos:
        # sockaddr の先頭要素がIPアドレス文字列（例: '93.184.216.34'）
        # 型チェッカーからは str か int か判別できないため str() で明示的に文字列化する
        ip_str = str(info[4][0])
        # IPv6のリンクローカルは 'fe80::1%en0' のようにゾーンIDが付くので切り落とす
        ip_str = ip_str.split("%")[0]

        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            # IPとして解釈できない＝想定外。安全側に倒して拒否する
            raise ValueError(f"このURLは取得できません: {url}") from exc

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
            raise ValueError(f"このURLは取得できません: {url}")

    # すべて公開IPだった＝安全と確認できたので、その url を返す
    return url


def _extract_url(url: str) -> str:
    """URLのウェブページから本文を抽出する。

    入力:
        url … 取得したいウェブページのURL

    出力:
        ページの表示テキスト（文字列）

    処理:
        取得の前に _ensure_safe_url で「公開サーバー向けのURLか」を検査し、
        検査を通った url だけを requests.get に渡す。
        危険なURLなら通信せずに ValueError を投げる。
    """
    # 通信する前に関門を通す（内部ネットワークへのアクセスを防ぐ＝SSRF対策）
    # 検査を通った url 自身を safe_url として受け取り、以降はこれだけを使う。
    # こうすることで「検証した値」と「requests.get へ渡す値」が同一になる。
    safe_url = _ensure_safe_url(url)

    # ページのHTMLを取得する
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
