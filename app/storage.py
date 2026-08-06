"""アップロードしたファイルの保存先（GCS / ローカル）を隠すストレージ層。

なぜこのモジュールが必要か:
    ルーターや本文抽出が SDK を直接呼ぶと、「保存する」「読む」「消す」処理の中に
    GCS固有のコードが散らばる。保存先を変えるたびに全箇所を直すことになり、
    どこか1つを直し忘れると、そこだけ古い保存先を見続ける。
    保存先の知識をこのモジュールに閉じ込め、外からは
    save / read_bytes / open_stream / delete / exists の5つだけを見せる。

呼び出し側が「どちらに保存されているか」を知らなくてよい:
    バックエンドの判断は _resolve_backend の1箇所だけで行う。
    sources_router も vectorizer も、GCSかローカルかを一切見ない。

安全性について（パストラバーサル対策）:
    受け取ったパス文字列は、必ず app/upload_paths.py の組み立て関数に通してから
    sink（open / GCSのSDK）へ渡す。検証を通らない名前は ValueError になり、
    ローカルなら UPLOAD_DIR の外、GCSなら定数プレフィックスの外には触れられない。
"""

import logging
from collections.abc import Iterator
from typing import Any

import google.cloud.storage as gcs
from google.cloud.exceptions import NotFound

from app.config import APP_ENV, DEV_ENVS, GCS_BUCKET_NAME
from app.upload_paths import build_safe_object_name, build_safe_upload_path

logger = logging.getLogger(__name__)

# ストリーミング配信で1回に読み出すバイト数。
# 大きなファイル（最大50MB）を一度にメモリへ載せないために分割して読む
STREAM_CHUNK_SIZE = 1024 * 1024  # 1MB

# GCSクライアント。import 時ではなく、最初に必要になった時点で作る（_get_bucket を参照）
_client: gcs.Client | None = None


def _resolve_backend(bucket_name: str, app_env: str) -> str:
    """どの保存先を使うかを決める。ここがバックエンド切り替えの唯一の分岐点。

    入力:
        bucket_name … GCSのバケット名（未設定なら空文字）
        app_env     … 実行環境の識別子（config の APP_ENV）

    出力:
        'gcs' … GCSに保存する
        'local' … ローカルの uploads/ に保存する

    例外:
        RuntimeError … 本番相当の環境なのにバケット名が未設定の場合

    なぜ本番相当では止めるのか（重要）:
        Cloud Run のコンテナはリクエストごとに別インスタンスへ振り分けられ、
        停止すると中身が消える。そこでローカル保存にフォールバックすると、
        アップロードは成功したように見えるのに、次のアクセスでファイルが消えている
        という状態になる。しかもエラーが出ないので誰も気づけない。

        設定漏れは「起動しない」という気づける形で表に出すべきなので、
        危険側（ローカル保存）へは倒さない。config の JWT_SECRET_KEY と同じ原則。
    """
    if bucket_name:
        return "gcs"

    if app_env in DEV_ENVS:
        # ローカル開発とテスト。全員がGCPの認証情報を持つとは限らないので、
        # バケット未設定なら uploads/ に保存して開発を止めない
        return "local"

    raise RuntimeError(
        f"GCS_BUCKET_NAME が設定されていません（APP_ENV={app_env or '未設定'}）。"
        "本番相当の環境ではローカルの uploads/ にフォールバックしません。"
        "コンテナのファイルシステムは停止時に消えるため、保存できたように見えて"
        "ファイルが失われます。バケット名を環境変数で渡してください。"
    )


# import 時に一度だけ判定し、設定漏れならこの時点で起動を止める（fail fast）。
# リクエストが来てから初めて落ちると、原因の切り分けが難しくなるため
_resolve_backend(GCS_BUCKET_NAME, APP_ENV)


def _current_backend() -> str:
    """現在の設定から保存先を決める（各操作の入口で呼ぶ）。

    import 時の値を定数に固定せず毎回引き直すのは、テストで
    GCS_BUCKET_NAME / APP_ENV を差し替えて両方の経路を確かめられるようにするため。
    """
    return _resolve_backend(GCS_BUCKET_NAME, APP_ENV)


def _get_bucket() -> Any:
    """GCSのバケットを返す（クライアントは初回だけ作る）。

    なぜ import 時にクライアントを作らないか:
        google.cloud.storage.Client() は生成時に認証情報を探しに行く。
        import 時に作ると、GCSを使わないテストやローカル開発でも
        認証情報が無いだけで import に失敗し、アプリ全体が起動しなくなる。
        既に app/vectorizer.py と app/vector_store.py が import 時に
        クライアントを作っており、そのせいでCIにダミーのAPIキーを
        置く必要が生じている（.github/workflows/ci.yml のコメント参照）。
        同じ作りにしない。

    バケット名について:
        常に config の GCS_BUCKET_NAME（コード側から見れば定数）を使う。
        オブジェクト名の側に何が入っても、別のバケットへは届かない。
    """
    global _client
    if _client is None:
        _client = gcs.Client()
    return _client.bucket(GCS_BUCKET_NAME)


def save(save_name: str, data: bytes) -> str:
    """ファイルの中身を保存し、DBの file_path に記録する文字列を返す。

    入力:
        save_name … サーバー側が組み立てた保存名（例: '<20桁>_<uuid4>.pdf'）
        data      … ファイルの中身（バイト列）

    出力:
        DBの file_path 列に記録する文字列（例: 'uploads/<保存名>'）

    例外:
        ValueError … save_name が保存時の生成規則に一致しない場合

    なぜGCSに保存する場合も 'uploads/...' 形式の文字列を返すのか:
        読み出す側は、この文字列からファイル名だけを取り出して
        （sanitize_upload_name が Path(...).name で前半を捨てる）
        保存先ごとの名前を組み立て直す。つまり前半のディレクトリ部分は
        元々「読み飛ばされる装飾」で、保存先の判断には使っていない。
        形式を揃えておけば、GCS移行の前後でDBの値の形が変わらず、
        既存データの移行が不要になる。
    """
    backend = _current_backend()

    if backend == "gcs":
        # 入力をそのまま渡さず、検証済みの要素から作り直した名前だけを渡す
        _get_bucket().blob(build_safe_object_name(save_name)).upload_from_string(data)
    else:
        safe_path = build_safe_upload_path(save_name)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_bytes(data)

    # DBに記録する文字列も、検証済みの名前から組み立て直す
    return str(build_safe_upload_path(save_name))


def read_bytes(raw_path: str) -> bytes:
    """保存済みファイルの中身を、まとめて読み込んで返す。

    入力:
        raw_path … DBに記録された保存パス文字列（信用しない値として扱う）

    出力:
        ファイルの中身（バイト列）

    例外:
        ValueError … 名前が保存時の生成規則に一致しない場合
        その他      … 実体が存在しない場合（FileNotFoundError / NotFound）

    使いどころ:
        本文抽出（app/vectorizer.py）。PDF等のライブラリは中身全体を必要とするため、
        逐次読み出しではなく一括で渡す。
    """
    if _current_backend() == "gcs":
        blob_bytes: bytes = _get_bucket().blob(build_safe_object_name(raw_path)).download_as_bytes()
        return blob_bytes

    return build_safe_upload_path(raw_path).read_bytes()


def open_stream(raw_path: str) -> Iterator[bytes]:
    """保存済みファイルの中身を、少しずつ読み出して返す（ストリーミング配信用）。

    入力:
        raw_path … DBに記録された保存パス文字列（信用しない値として扱う）

    出力:
        バイト列を順番に返すイテレータ

    例外:
        ValueError … 名前が保存時の生成規則に一致しない場合

    なぜ一括で読まないか:
        ファイルは最大50MB。ダウンロードのたびに全体をメモリへ載せると、
        同時アクセスが重なったときにメモリを圧迫する。
        1MBずつ読み出して、読んだ分から順に応答へ流す。

    なぜ署名付きURLを使わないか:
        署名付きURLは、URLさえ持っていればアプリの権限チェックを通らずに
        ファイルを取得できてしまう。CLAUDE.md の権限ルール
        「他人の個別ソースは絶対不可」と噛み合わないため、
        MVPではサーバー経由の配信にして権限判定を必ず通す。
    """
    if _current_backend() == "gcs":
        blob = _get_bucket().blob(build_safe_object_name(raw_path))
        with blob.open("rb") as stream:
            while chunk := stream.read(STREAM_CHUNK_SIZE):
                yield chunk
        return

    with open(build_safe_upload_path(raw_path), "rb") as f:
        while chunk := f.read(STREAM_CHUNK_SIZE):
            yield chunk


def exists(raw_path: str) -> bool:
    """保存済みファイルの実体があるかどうかを返す。

    入力:
        raw_path … DBに記録された保存パス文字列（信用しない値として扱う）

    出力:
        True … 実体がある / False … 実体が無い

    例外:
        ValueError … 名前が保存時の生成規則に一致しない場合

    補足:
        「名前が不正」と「実体が無い」は区別して扱う。
        前者は想定外の値が入り込んでいるので例外にし、呼び出し側が記録できるようにする。
        後者は起こりうる状態（手動削除など）なので False を返す。
    """
    if _current_backend() == "gcs":
        blob_exists: bool = _get_bucket().blob(build_safe_object_name(raw_path)).exists()
        return blob_exists

    return build_safe_upload_path(raw_path).is_file()


def delete(raw_path: str) -> None:
    """保存済みファイルの実体を削除する。

    入力:
        raw_path … DBに記録された保存パス文字列（信用しない値として扱う）

    出力:
        なし

    例外:
        ValueError … 名前が保存時の生成規則に一致しない場合

    実体が無いときに例外にしない理由（冪等にする）:
        削除は「消えた状態にする」のが目的で、呼ばれた時点で既に消えていても
        目的は達成されている。ここで例外にすると、ロールバックや再削除のたびに
        呼び出し側が同じ握りつぶし処理を書くことになる。
    """
    if _current_backend() == "gcs":
        try:
            _get_bucket().blob(build_safe_object_name(raw_path)).delete()
        except NotFound:
            # 既に無い＝目的は達成されている。消し直す必要はない
            logger.info("削除対象のオブジェクトは既に存在しませんでした")
        return

    safe_path = build_safe_upload_path(raw_path)
    # missing_ok=True … 既に無い場合も例外にしない
    safe_path.unlink(missing_ok=True)
