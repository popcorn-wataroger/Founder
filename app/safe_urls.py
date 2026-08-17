"""URLの安全性検証を一箇所に集約するモジュール。

何のためにあるか:
    社長がフォームに入力したURLは、そのままでは信用できない値。
    何の検査もせずに通信すると、サーバー自身に「内部だけに見えるアドレス」へ
    アクセスさせられてしまう（SSRF＝サーバー側リクエスト偽造）。
    たとえば http://169.254.169.254/ はクラウド(GCP等)のメタデータサーバーで、
    サービスアカウントのアクセストークンが取れてしまう。
    http://localhost:8000/ や社内ネットワークのIPも同様に危険。

    URLを扱う場所が増えても検査の書き写しが起きないよう、
    「URLが安全か」を判断する処理はこのモジュールにだけ置く。

方針（app/upload_paths.py と同じ考え方）:
    入力された値そのものを sink（requests.get など、実際に外部へ影響する処理）へ
    渡さない。urlsplit で要素に分解し、個々の要素を検査したうえで、
    検証を通った要素だけから新しいURLを組み立て直して返す。
    upload_paths.py が「ユーザーのファイル名を使わずサーバー側で保存名を作り直す」のと
    同じ形で、汚染された入力が sink へ届く経路を構造的に断つ。
    静的解析(CodeQL)にも、検証を通過した値であることが伝わる。
"""

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

# URL取得時のタイムアウト秒数（応答が無いURLで固まらないための保険）
URL_TIMEOUT = 10


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


def build_safe_public_url(raw_url: str) -> str:
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

    # https 以外はすべて拒否する。
    # http を許さない理由:
    #     通信内容が暗号化されないため、経路上で書き換えられた内容をそのまま
    #     ベクトル化して社内ナレッジに取り込んでしまう恐れがある。
    #     また http:// は内部サービス（http://127.0.0.1:8000/ など）を
    #     指す用途で使われやすく、SSRF の入口になりやすい。
    if parsed.scheme.lower() != "https":
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
