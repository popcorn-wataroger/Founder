import logging
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import storage
from app.database import get_connection
from app.routers.auth_router import (
    ROLE_CEO,
    require_business_user,
    require_ceo,
    require_source_uploader,
)
from app.safe_urls import build_safe_public_url
from app.upload_paths import build_save_name, sanitize_upload_name
from app.vector_store import delete_by_source_id, ensure_collection, save_chunks
from app.vectorizer import embed_text, extract_text, split_into_chunks

router = APIRouter(prefix="/api/sources", tags=["sources"])

# 管理者専用のソースAPIをまとめるルーター。URLは /api/admin から始まる
# （社長が社員データ画面で、その社員の個別ソースだけを見るために使う）
admin_router = APIRouter(prefix="/api/admin", tags=["sources"])

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# アップロードを読み進める単位（1MB）。
# 一度に全体を読まず、この単位で区切って累計サイズを確かめる
UPLOAD_CHUNK_SIZE = 1024 * 1024

# アップロードの入口で受け付ける拡張子。ユーザーが送ってきたファイル名を
# ここで弾くための検査用なので、保存名の組み立て（app/upload_paths.py）とは役割が別
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".txt"}

# ダウンロード時に返す Content-Type。表示名（file_name）からの推測ではなく
# ソース種別の定数表から引く。表示名に拡張子が無くても正しい種別で返せる
MEDIA_TYPE_BY_TYPE = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain; charset=utf-8",
}

# ダウンロード時の表示名から取り除く制御文字（改行・タブ・NUL など）。
# ヘッダに載せる文字列を1行に閉じ込めるために使う
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


@router.get("")
async def list_sources(token: dict = Depends(require_ceo)):
    """ソース一覧を返す"""
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY uploaded_at DESC").fetchall()
    return [dict(row) for row in rows]


@router.get("/common")
async def list_common_sources(
    token: dict = Depends(require_source_uploader),
) -> list[dict[str, Any]]:
    """全社共通ソースだけを一覧で返す（社長・共通ソース管理者が使う）。

    入力:
        token … require_source_uploader が返すログイン情報
                （ceo か source_manager でなければ403で弾かれている）

    出力:
        共通ソースの一覧（登録日の新しい順）。
        各件に source_id / file_name / file_type / uploaded_at を含む

    使いどころ:
        共通ソース管理者の専用画面（#screen-source-manager）の登録済み一覧。
        自分が登録したものを確認し、二重登録に気づけるようにするための欄。

    既存の GET /api/sources と分けた理由:
        GET /api/sources は SELECT * で全ソースを返すため、他人の個別ソースの
        ファイル名（「◯◯_評価2025.pdf」のように名前自体が見せてはいけない情報）や
        file_path が含まれる。既存APIの依存を緩めて role で分岐させると、
        1つの関数が2つの権限を持つことになり、条件を書き漏らした時の被害が大きい。
        POST /api/sources/my-upload と同じく、入口を分けてそれぞれが1つのことだけを行う。

    絞り込み条件について（権限ルール）:
        WHERE scope = 'common' を必ず付ける。この条件が無いと個別ソースが混ざる。
        owner_user_id は共通ソースでは常に NULL なので条件に使わない。

    file_path を返さない理由:
        サーバー内部の保存先パスで画面には不要。
        GET /api/admin/users/{user_id}/sources と同じ方針。
    """
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT source_id, file_name, file_type, uploaded_at
            FROM sources
            WHERE scope = 'common'
            ORDER BY uploaded_at DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]


@admin_router.get("/users/{user_id}/sources")
async def list_user_sources(
    user_id: str, token: dict = Depends(require_ceo)
) -> list[dict[str, Any]]:
    """指定した社員の「個別ソース」だけを一覧で返す（社長専用）。

    入力:
        user_id … ソースを見たい社員の user_id（URLパスで指定）
        token   … require_ceo が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        その社員の個別ソースの一覧（登録日の新しい順）。
        各件に source_id / file_name / file_type / scope / owner_user_id / uploaded_at を含む

    使いどころ:
        社員データ画面の「過去のソース」欄。その社員に紐づく資料だけを並べる。

    絞り込み条件について（権限ルール）:
        WHERE に scope='individual' と owner_user_id=%s の両方を必ず付ける。
        - owner_user_id だけで絞ると、共通ソースは owner_user_id が NULL なので
          混ざりはしないが、条件が「持ち主」だけになり意図が読み取りづらい
        - scope だけで絞ると、他人の個別ソースまで出てしまう（絶対に不可）
        この欄は「その社員の資料」を並べる場所なので、全社共通ソースも出さない。

    file_path を返さない理由:
        file_path はサーバー内部の保存先パスで、画面には不要。
        外に出すと保存場所の構造が分かってしまうため、返す列を明示的に選んでいる。
    """
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """
            SELECT source_id, file_name, file_type, scope, owner_user_id, uploaded_at
            FROM sources
            WHERE scope = 'individual' AND owner_user_id = %s
            ORDER BY uploaded_at DESC
            """,
            (user_id,),
        ).fetchall()

    return [dict(row) for row in rows]


VALID_SCOPES = {"common", "individual"}


def validate_scope(scope: str, owner_user_id: str | None) -> None:
    if scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail="scope は common または individual を指定してください",
        )
    if scope == "individual" and not owner_user_id:
        raise HTTPException(status_code=400, detail="individual の場合は owner_user_id が必須です")


def check_scope_permission(role: str, scope: str) -> None:
    """登録しようとしているソース種別（scope）を、その役割（role）が扱えるかを確かめる。

    入力:
        role  … ログインしている人のロール名（JWTの role）
        scope … 登録しようとしているソース種別（'common' か 'individual'）

    処理:
        scope が 'individual' のときだけ、role が ROLE_CEO かどうかを見る。
        個別ソースは特定の社員に紐づく資料（評価・給与など）なので、社長だけに許す。
        scope が 'common' のときは何もしない。
        入口の require_source_uploader で ceo / source_manager に絞り込み済みで、
        そのどちらも共通ソースを登録してよいため、ここで重ねて弾く必要がない。

    出力:
        なし。問題があれば HTTPException(403) を投げる。
        「返り値で可否を伝える」形にしないのは、呼び出し側が結果を見忘れても
        処理が進んでしまう余地を残さないため（例外なら必ずそこで止まる）。

    なぜ既存の validate_scope() に混ぜず別関数にしたか:
        validate_scope は「リクエストの形式が正しいか」を見る関数で、
        おかしければ 400（クライアントの送り方が悪い）を返す。
        こちらは「その人に権限があるか」を見る関数で、無ければ 403（送り方は正しいが
        許可されていない）を返す。400 と 403 は利用者に伝える意味が異なるため、
        同じ関数に同居させると、名前から中身が予想できなくなる。
        呼ぶ順番も「形式の検証 → 権限の判定」で固定できる。

    なぜ Depends ではなく関数内で判定するのか:
        require_source_uploader のような Depends はリクエストのヘッダ（JWT）だけで
        判定できるので、ハンドラに入る前に実行できる。
        一方 scope はフォームやリクエストボディの値で、Depends が動く時点では
        まだ読み取っていない。そのため scope を使う判定は、
        値が揃ったあとのハンドラ内部で呼ぶ必要がある。
    """
    if scope == "individual" and role != ROLE_CEO:
        raise HTTPException(status_code=403, detail="個別ソースを登録できるのは管理者のみです")


async def read_upload_within_limit(file: UploadFile) -> bytes:
    """アップロードされたファイルを、上限を超えた時点で打ち切りながら読み込む。

    入力:
        file … FastAPI が受け取った UploadFile

    出力:
        読み込んだファイル全体のバイト列（MAX_FILE_SIZE 以内）

    例外:
        HTTPException(413) … 累計サイズが MAX_FILE_SIZE を超えた場合

    処理:
        UPLOAD_CHUNK_SIZE ずつ読み進め、読むたびに累計サイズを確かめる。
        上限を超えた時点で残りを読まずに 413 を投げる。

    なぜ一度に read() しないか:
        `await file.read()` は上限判定より先にファイル全体をメモリへ載せる。
        50MB上限に対して1GBのファイルを送られると、拒否する前に1GBを消費し、
        同時に複数送られると Cloud Run のメモリ上限に達してコンテナが再起動する。
        再起動は他のリクエストも巻き込むため、「拒否する分は読まない」形にする。

    なぜ両エンドポイントで共通の関数にするか:
        upload_source() と upload_my_source() で判定がずれると、
        入口によって通るサイズが違うという説明のつかない状態になる。
        判定を1箇所に集約して「唯一の正解」にする。
    """
    chunks: list[bytes] = []
    total_size = 0

    while True:
        chunk = await file.read(UPLOAD_CHUNK_SIZE)
        if not chunk:
            break

        total_size += len(chunk)
        # 上限を超えたら、残りを読まずにここで打ち切る
        if total_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="ファイルサイズが50MBを超えています")

        chunks.append(chunk)

    return b"".join(chunks)


def _sanitize_for_log(value: object) -> str:
    """ログに書き出す値から改行を取り除く（ログインジェクション対策）。

    入力:
        value … ログに載せたい値（source_id など、リクエスト由来の可能性がある値）

    出力:
        改行を取り除いた文字列

    なぜ必要か（ログインジェクション対策）:
        ログは1行1レコードとして読まれる。値の中に改行が混ざっていると、
        攻撃者が「偽のログ行」を丸ごと差し込めてしまう。たとえば
        '1\\nINFO: 管理者がログインしました' のような値を送られると、
        ログ上は正規の記録と見分けがつかず、調査や監査を欺かれる。
        そこで、値をログに渡す前に改行を落として「1行に閉じ込める」。

    補足:
        現在の delete_source は source_id を int で受けており、数値以外は
        FastAPI が422で弾くため実際には改行は入らない。それでもここを通すのは、
        将来 str で受けるよう変わったときに黙って穴が開くのを防ぐため。
    """
    return str(value).replace("\r", "").replace("\n", "")


async def _rollback_source(source_id: int, file_path: str | None) -> None:
    """ベクトル化に失敗したとき、DB登録とファイル保存を取り消す。

    入力:
        source_id … 取り消したいソースのID
        file_path … 保存したファイルのキー（URL登録などファイルが無い場合は None）

    出力:
        なし

    なぜロールバックするか:
        ベクトル化に失敗したままDBに行を残すと、
        「一覧には出るのにAIが中身を知らないソース」ができてしまう。
        社長から見ると登録できたように見えるのに、誰が質問しても答えられない。
        中途半端な状態を残さないよう、失敗時は登録前の状態まで戻す。

    なぜ Path ではなく文字列を受け取るか:
        保存先はローカルとは限らない（GCSにはパスではなくオブジェクト名がある）。
        「どこに保存されているか」は app/storage.py だけが知っていればよいので、
        ここでは storage が解釈できるキーを、そのまま storage.delete に渡す。

    なぜ async にしているか:
        storage.delete は同期関数で、GCS が保存先のときは中で通信する。
        呼び出し元は async のルーターなので、そのまま呼ぶと通信を待つ間
        イベントループが止まり、他のリクエストが待たされる。
        run_in_threadpool で別スレッドへ逃がすため、この関数も async にしている。
    """
    # DBの登録を取り消す
    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM sources WHERE source_id = %s", (source_id,))
        conn.commit()

    # 保存した実体も消す（ファイルが無い登録＝URLの場合はスキップ）
    #
    # 失敗しても例外を投げ直さない理由:
    #     この関数はベクトル化に失敗したあとの後始末として呼ばれる。
    #     ここで例外が漏れると、呼び出し側が本来返したかった
    #     「ベクトル化に失敗した」というエラーが、削除の失敗で塗り潰されてしまう。
    #     利用者から見た原因が変わってしまうため、削除の失敗は握って続行する。
    #
    #     DBの行は既に消えているので、残るのは参照されない実体だけ
    #     （GCSなら孤児オブジェクト、ローカルなら uploads/ の残骸）。
    #     権限や整合性の問題は起こさないが、放置すると容量を食うため
    #     手作業で消せるようにログへ file_path を残す。
    if file_path is not None:
        try:
            await run_in_threadpool(storage.delete, file_path)
        except Exception:
            # file_path はDB由来の値。ログ改行インジェクションを防ぐため必ず通す
            logger.exception(
                "ロールバック中のファイル削除に失敗しました。"
                "参照されない実体が残っている可能性があります source_id=%s file_path=%s",
                source_id,
                _sanitize_for_log(file_path),
            )


async def _cleanup_orphan_file(file_path: str) -> None:
    """DB登録に失敗したとき、保存済みのファイルだけを削除する。

    入力:
        file_path … 保存したファイルのキー（storage が解釈できる値）

    出力:
        なし

    _rollback_source との違い:
        _rollback_source は「DB登録に成功したあと」の失敗を扱うので、
        DBの行を消してからファイルを消す。
        こちらは「DB登録そのものに失敗した」場合なので、消すべき行が存在しない。
        DELETE を投げると、まだ他の誰も知らない source_id を
        当てずっぽうで消しにいくことになるため、ファイルだけを対象にする。

    なぜ例外を投げ直さないか:
        この関数は、DB登録が失敗したという本来のエラーの後始末として呼ばれる。
        ここで例外が漏れると、呼び出し側が伝えたかった原因（DB登録の失敗）が
        削除の失敗で塗り潰され、調査の入口が変わってしまう。
        残るのは参照されない実体だけなので、手作業で消せるようログに残して続行する。
    """
    try:
        await run_in_threadpool(storage.delete, file_path)
    except Exception:
        logger.exception(
            "DB登録の失敗後、保存済みファイルの削除に失敗しました。"
            "参照されない実体が残っている可能性があります file_path=%s",
            _sanitize_for_log(file_path),
        )


def _vectorize_and_save(
    source_id: int,
    path: str,
    file_type: str,
    scope: str,
    owner_user_id: str | None,
) -> int:
    """ソースの本文を取り出してベクトル化し、Qdrantに保存する。

    入力:
        source_id     … DBに登録したソースのID（Qdrant側にも紐付けて保存する）
        path          … 本文の取得元。ファイルパス、file_type='url' ならURL文字列
        file_type     … 'pdf' / 'docx' / 'pptx' / 'txt' / 'url'
        scope         … 'common'（全社共通）か 'individual'（社員個別）
        owner_user_id … 個別ソースの持ち主。共通ソースなら None

    出力:
        Qdrantに保存したチャンク数

    処理:
        1. コレクション（棚）とインデックスを用意する
        2. 本文テキストを取り出す
        3. 500文字ごとのチャンクに分割する
        4. チャンクごとにベクトル化する（Gemini APIを呼ぶ）
        5. Qdrantに保存する。権限フィルタ用に scope / owner_user_id も一緒に持たせる

    失敗した場合:
        例外はそのまま呼び出し元へ投げる。呼び出し元がロールバックを行う。
    """
    # 1. 棚とインデックスを用意する（既にあれば何も起きない）
    ensure_collection()

    # 2. ファイル（またはURL）から本文テキストを取り出す
    text = extract_text(path, file_type)

    # 3. 検索しやすいよう、本文を500文字ごとのチャンクに分割する
    chunks = split_into_chunks(text)

    # 本文が空（白紙PDFなど）だとチャンクが0件になり、ベクトル化するものが無い
    # 検索に一切出ないソースを登録しても意味がないので、失敗として扱う
    if not chunks:
        raise ValueError("本文テキストを抽出できませんでした")

    # 4. チャンクごとにベクトル化する（チャンク数に比例して時間がかかる）
    vectors = [embed_text(chunk) for chunk in chunks]

    # 5. Qdrantに保存する。source_id は文字列にして渡す（DBは整数、payloadは文字列で扱う）
    save_chunks(
        chunks=chunks,
        vectors=vectors,
        source_id=str(source_id),
        scope=scope,
        owner_user_id=owner_user_id,
    )

    return len(chunks)


@router.post("/upload")
async def upload_source(
    file: UploadFile,
    scope: str = Form("common"),
    owner_user_id: str | None = Form(None),
    token: dict = Depends(require_source_uploader),
):
    """ファイルをアップロードしてソースとして登録し、AIが検索できるようベクトル化する。

    誰が使えるか（権限ルール）:
        入口の require_source_uploader で ceo / source_manager に絞る。
        そのうえで、個別ソース（scope='individual'）の登録は
        check_scope_permission が ceo だけに限定する。
        つまり source_manager は共通ソースだけを登録できる。

    処理:
        1. 入力チェック（scope、ファイル名、拡張子、サイズ）と scope の権限判定
        2. ストレージにファイルを保存する（保存先がGCSかローカルかは storage が決める）
        3. sources テーブルに登録し、source_id を採番する
        4. 本文を取り出してベクトル化し、Qdrantに保存する
        5. 4が失敗したら、3のDB登録と2のファイル保存を取り消して（ロールバック）エラーを返す

    なぜ順番が「DB登録 → ベクトル化」なのか:
        Qdrantに保存するとき、どのソース由来かを示す source_id が必要になる。
        source_id はDBに登録して初めて採番される
        （GENERATED ALWAYS AS IDENTITY）ため、先にDB登録する。
    """
    # 形式の検証（400）が先、権限の判定（403）が後。
    # scope が common / individual のどちらかであると確定してから権限を見る
    validate_scope(scope, owner_user_id)
    check_scope_permission(token["role"], scope)

    if not file.filename:
        raise HTTPException(status_code=400, detail="ファイル名が取得できません")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"対応していないファイル形式です。対応形式: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    contents = await read_upload_within_limit(file)

    # ソース種別（pdf / docx / pptx / txt）。上のチェックを通っているので必ず対応表に存在する
    file_type = suffix.lstrip(".")

    # 保存名は、ユーザーのファイル名を一切使わずサーバー側で組み立てる（パストラバーサル対策）。
    # 組み立ての中身と、その理由は app/upload_paths.py の build_save_name にまとめている。
    # 保存名を検証する側（sanitize_upload_name）と同じファイルに置くことで、
    # 片方だけ形式が変わってダウンロードが404になる、というズレを防いでいる
    save_name = build_save_name(file_type)

    # 保存する。保存先がGCSかローカルかは storage が判断するので、ここでは意識しない。
    # 戻り値の file_path は、後で読み出すときに storage へ渡すキー（DBにも記録する）
    # storage.save は同期関数で、GCS が保存先のときは中で通信する。
    # async のルーターから直接呼ぶと通信待ちの間イベントループが止まるため、
    # 別スレッドへ逃がす（他のリクエストの処理を止めない）
    try:
        file_path = await run_in_threadpool(storage.save, save_name, contents)
    except Exception:
        # ファイル名はユーザー入力なので、改行を落としてからログに渡す
        logger.exception(
            "ファイルの保存に失敗しました file_name=%s", _sanitize_for_log(file.filename)
        )
        raise HTTPException(status_code=500, detail="ファイルの保存に失敗しました")

    uploaded_at = datetime.now(timezone.utc).isoformat()

    # ここから先が失敗すると、保存済みのファイルだけが残って誰からも参照されなくなる。
    # DBに行が無いので画面にも出ず、削除する手段も無い。
    # 例外はそのまま外へ投げつつ、保存した実体だけを消してから通す
    try:
        with closing(get_connection()) as conn:
            # RETURNING で採番された source_id を受け取る。値は commit の前に fetchone() で取り出す
            cursor = conn.execute(
                """
                INSERT INTO sources
                    (file_name, file_type, file_path, scope,
                     owner_user_id, uploaded_at, uploaded_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING source_id
                """,
                (
                    # file_name … 画面に表示する「元のファイル名」。
                    #              表示専用で、パスの組み立てには使わない
                    # file_path … 読み出しに使うキー。上で生成した安全な名前だけから作られている
                    # 「見せる名前」と「保存先の名前」を分けることでパストラバーサルを断つ
                    file.filename,
                    file_type,
                    file_path,
                    scope,
                    owner_user_id,
                    uploaded_at,
                    token["user_id"],
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            source_id = row["source_id"] if row else None

        # INSERT が成功していれば RETURNING は必ず1行返す。
        # None なら採番できていない＝本来ありえない状態なので、ここで止める
        if source_id is None:
            raise RuntimeError(
                f"sources への INSERT で source_id が採番されませんでした file_name={file.filename}"
            )
    except Exception:
        logger.exception(
            "ソースのDB登録に失敗しました。保存済みファイルを削除します file_name=%s",
            _sanitize_for_log(file.filename),
        )
        await _cleanup_orphan_file(file_path)
        raise

    # 本文を取り出してベクトル化し、Qdrantに保存する（ここまで成功して初めて「登録完了」）
    try:
        chunk_count = _vectorize_and_save(
            source_id=source_id,
            path=file_path,
            file_type=file_type,
            scope=scope,
            owner_user_id=owner_user_id,
        )
    except Exception:
        # 失敗したら、直前のDB登録とファイル保存を取り消して登録前の状態に戻す
        logger.exception(
            "ベクトル化に失敗しました。ソース登録を取り消します source_id=%s", source_id
        )
        await _rollback_source(source_id, file_path)
        raise HTTPException(
            status_code=500,
            detail="ファイルの読み取りまたはベクトル化に失敗しました。ソースは登録されていません",
        )

    return {
        "success": True,
        "source_id": source_id,
        "file_name": file.filename,
        "chunk_count": chunk_count,
    }


@router.post("/my-upload")
async def upload_my_source(
    file: UploadFile,
    token: dict = Depends(require_business_user),
) -> dict[str, Any]:
    """自分の個別ソースとしてファイルを登録し、AIが検索できるようベクトル化する。

    誰が使えるか（権限ルール）:
        ログイン中の社員本人。登録できるのは「自分の個別ソース」だけで、
        他人の資料としては登録できない。
        入口の依存は require_business_user なので、システム管理者(admin)を除く
        ログイン済みユーザーなら誰でも使える
        （社員・source_manager・社長のいずれも、自分の資料を上げられる）。
        システム管理者(admin)はアカウント管理だけを担当し、業務データを持たない役割なので、
        入口の require_business_user で403になる。

    処理:
        1. 入力チェック（ファイル名、拡張子、サイズ）
        2. ストレージにファイルを保存する（保存先がGCSかローカルかは storage が決める）
        3. sources テーブルに登録し、source_id を採番する
        4. 本文を取り出してベクトル化し、Qdrantに保存する
        5. 4が失敗したら、3のDB登録と2のファイル保存を取り消して（ロールバック）エラーを返す

    なぜ scope と owner_user_id を引数で受け取らないか（重要）:
        受け取ると、他人の user_id を送るだけで他人の資料として登録できてしまう。
        引数に無ければ、そもそも他人を指す値が入り込む経路が存在しない。
        「送られてきた値を検証で弾く」のではなく、「値を送れる経路そのものを作らない」。
        scope は 'individual' 固定、owner_user_id は token（JWT）の user_id 固定で、
        どちらもリクエストからは決められない。

    なぜ既存の upload_source() に分岐を足さないのか（重要）:
        あちらは require_source_uploader で守られた共通ソース用の入口。
        同じ関数に「社員なら owner_user_id を強制上書きする」という分岐を足すと、
        条件が増えるほど権限の穴を見落としやすくなる。
        入口を分けて、それぞれが1つのことだけをする形にしておけば、
        「この入口では他人の資料に触れない」を関数ごとに読み切れる。

    なぜ順番が「DB登録 → ベクトル化」なのか:
        Qdrantに保存するとき、どのソース由来かを示す source_id が必要になる。
        source_id はDBに登録して初めて採番される
        （GENERATED ALWAYS AS IDENTITY）ため、先にDB登録する。

    validate_scope / check_scope_permission を呼ばない理由:
        どちらもリクエストで指定された scope と owner_user_id を検査する関数。
        この入口ではその2つがリクエストから来ないため、検証する対象がない。
    """
    # scope と owner_user_id はサーバー側で固定する（リクエストからは決められない）
    scope = "individual"
    owner_user_id = token["user_id"]

    if not file.filename:
        raise HTTPException(status_code=400, detail="ファイル名が取得できません")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"対応していないファイル形式です。対応形式: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    contents = await read_upload_within_limit(file)

    # ソース種別（pdf / docx / pptx / txt）。上のチェックを通っているので必ず対応表に存在する
    file_type = suffix.lstrip(".")

    # 保存名は、ユーザーのファイル名を一切使わずサーバー側で組み立てる（パストラバーサル対策）。
    # 組み立ての中身と、その理由は app/upload_paths.py の build_save_name にまとめている
    save_name = build_save_name(file_type)

    # 保存する。保存先がGCSかローカルかは storage が判断するので、ここでは意識しない。
    # storage.save は同期関数で、GCS が保存先のときは中で通信する。
    # async のルーターから直接呼ぶと通信待ちの間イベントループが止まるため、別スレッドへ逃がす
    try:
        file_path = await run_in_threadpool(storage.save, save_name, contents)
    except Exception:
        # ファイル名はユーザー入力なので、改行を落としてからログに渡す
        logger.exception(
            "ファイルの保存に失敗しました file_name=%s", _sanitize_for_log(file.filename)
        )
        raise HTTPException(status_code=500, detail="ファイルの保存に失敗しました")

    uploaded_at = datetime.now(timezone.utc).isoformat()

    # ここから先が失敗すると、保存済みのファイルだけが残って誰からも参照されなくなる。
    # 例外はそのまま外へ投げつつ、保存した実体だけを消してから通す
    try:
        with closing(get_connection()) as conn:
            # RETURNING で採番された source_id を受け取る。値は commit の前に fetchone() で取り出す
            cursor = conn.execute(
                """
                INSERT INTO sources
                    (file_name, file_type, file_path, scope,
                     owner_user_id, uploaded_at, uploaded_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING source_id
                """,
                (
                    # 「見せる名前」と「保存先の名前」を分けることでパストラバーサルを断つ
                    file.filename,
                    file_type,
                    file_path,
                    scope,
                    owner_user_id,
                    uploaded_at,
                    token["user_id"],
                ),
            )
            row = cursor.fetchone()
            conn.commit()
            source_id = row["source_id"] if row else None

        # INSERT が成功していれば RETURNING は必ず1行返す。
        # None なら採番できていない＝本来ありえない状態なので、ここで止める
        if source_id is None:
            raise RuntimeError(
                f"sources への INSERT で source_id が採番されませんでした file_name={file.filename}"
            )
    except Exception:
        logger.exception(
            "ソースのDB登録に失敗しました。保存済みファイルを削除します file_name=%s",
            _sanitize_for_log(file.filename),
        )
        await _cleanup_orphan_file(file_path)
        raise

    # 本文を取り出してベクトル化し、Qdrantに保存する（ここまで成功して初めて「登録完了」）
    try:
        chunk_count = _vectorize_and_save(
            source_id=source_id,
            path=file_path,
            file_type=file_type,
            scope=scope,
            owner_user_id=owner_user_id,
        )
    except Exception:
        # 失敗したら、直前のDB登録とファイル保存を取り消して登録前の状態に戻す
        logger.exception(
            "ベクトル化に失敗しました。ソース登録を取り消します source_id=%s", source_id
        )
        await _rollback_source(source_id, file_path)
        raise HTTPException(
            status_code=500,
            detail="ファイルの読み取りまたはベクトル化に失敗しました。ソースは登録されていません",
        )

    return {
        "success": True,
        "source_id": source_id,
        "file_name": file.filename,
        "chunk_count": chunk_count,
    }


class UrlRequest(BaseModel):
    url: str
    file_name: str | None = None
    scope: str = "common"
    owner_user_id: str | None = None


@router.post("/url")
async def register_url(req: UrlRequest, token: dict = Depends(require_source_uploader)):
    """URLをソースとして登録し、AIが検索できるようベクトル化する。

    誰が使えるか（権限ルール）:
        upload_source と同じ。入口の require_source_uploader で
        ceo / source_manager に絞り、個別ソースの登録は
        check_scope_permission が ceo だけに限定する。

    処理:
        1. 入力チェック（scope）と scope の権限判定
        2. URLを検証し、検証を通った要素だけで組み立て直す（build_safe_public_url）
        3. sources テーブルに登録し、source_id を採番する
        4. URLのページから本文を取り出してベクトル化し、Qdrantに保存する
        5. 4が失敗したら、3のDB登録を取り消して（ロールバック）エラーを返す

    ファイルアップロードとの違い:
        URLの場合は実ファイルを保存しないので、file_path には検証済みのURLを入れる。
        ロールバック時も消すファイルが無いため、_rollback_source に save_path=None を渡す。
        本文の取得は extract_text が file_type='url' としてページを取りに行く。
    """
    # 形式の検証（400）が先、権限の判定（403）が後。upload_source と同じ順番
    validate_scope(req.scope, req.owner_user_id)
    check_scope_permission(token["role"], req.scope)

    # DB登録の前にURLを検証する（SSRF対策）。
    #
    # なぜ INSERT より前に置くか:
    #     以前は startswith で前方一致を見るだけで、実質的な検証（スキーム・
    #     内部IP・認証情報）はベクトル化の段階まで遅延していた。
    #     そのため不正なURLでも一度DBに行が作られ、失敗してからロールバックされる。
    #     ロールバック自体が失敗すれば、不正なURLの行が残る。
    #     入口で弾けば、そもそも書き込みが起きない。
    #
    # なぜ検証済みの safe_url をDBと _vectorize_and_save の両方に渡すか:
    #     生の req.url を保存すると、後で再取得するときに未検証の値が
    #     使われることになる。検証を通った値だけを流す。
    try:
        safe_url = build_safe_public_url(req.url)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="有効なURLを入力してください。https:// で始まる外部の公開URLを指定してください",
        )

    display_name = req.file_name or safe_url
    uploaded_at = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as conn:
        # RETURNING で採番された source_id を受け取る。値は commit の前に fetchone() で取り出す
        cursor = conn.execute(
            """
            INSERT INTO sources
                (file_name, file_type, file_path, scope, owner_user_id, uploaded_at, uploaded_by)
            VALUES (%s, 'url', %s, %s, %s, %s, %s)
            RETURNING source_id
            """,
            (display_name, safe_url, req.scope, req.owner_user_id, uploaded_at, token["user_id"]),
        )
        row = cursor.fetchone()
        conn.commit()
        source_id = row["source_id"] if row else None

    # INSERT が成功していれば RETURNING は必ず1行返す。
    # None なら採番できていない＝本来ありえない状態なので、ここで止める
    if source_id is None:
        raise RuntimeError(
            f"sources への INSERT で source_id が採番されませんでした url={safe_url}"
        )

    # URLのページ本文を取り出してベクトル化し、Qdrantに保存する
    # （ここまで成功して初めて「登録完了」。ファイルアップロードと同じ扱い）
    try:
        chunk_count = _vectorize_and_save(
            source_id=source_id,
            path=safe_url,  # URLの場合、本文の取得元は検証済みのURL
            file_type="url",
            scope=req.scope,
            owner_user_id=req.owner_user_id,
        )
    except Exception:
        # 失敗したら、直前のDB登録を取り消して登録前の状態に戻す
        # URLは実ファイルを保存していないので save_path は None
        logger.exception("ベクトル化に失敗しました。URL登録を取り消します source_id=%s", source_id)
        await _rollback_source(source_id, None)
        raise HTTPException(
            status_code=500,
            detail="URLの読み取りまたはベクトル化に失敗しました。ソースは登録されていません",
        )

    return {
        "success": True,
        "source_id": source_id,
        "url": safe_url,
        "chunk_count": chunk_count,
    }


def _safe_download_name(raw_name: object, fallback: str) -> str:
    """ダウンロード時にブラウザへ提示する表示名を、安全な形に整えて返す。

    入力:
        raw_name … DBの file_name（アップロード時の元のファイル名＝完全なユーザー入力）
        fallback … raw_name が使い物にならなかったときに使う代替名（ディスク上の安全な名前）

    出力:
        Content-Disposition ヘッダに載せてよい表示名

    なぜ必要か（ヘッダインジェクション対策）:
        file_name はクライアントが自由に決められる文字列で、DBにそのまま入っている。
        これをヘッダに載せる際、改行が混ざるとヘッダを分割して別のヘッダを
        差し込まれる恐れがある。

        実際のエンコードは build_content_disposition が行う（quote に通し、
        変化した場合は RFC5987 形式に切り替える）ので、改行・ダブルクォート・
        非ASCIIはそこでパーセントエンコードされる。

        ただし '/' は quote の既定で素通りするため、'a/b.pdf' のような値は
        filename="a/b.pdf" とディレクトリ区切りを含んだまま提示されてしまう。
        表示名として不適切なので、ここで区切り文字と制御文字を落としておく。
        エンコード側の実装に安全性を全面的に預けない、という意味の多層防御でもある。

    注意:
        この関数が返すのは「見せる名前」だけで、実際に読む保存先の名前とは無関係。
        保存先の名前は app/upload_paths.py の組み立て関数が別に作る。
    """
    # '/' と '\' の両方を区切りとみなし、最後の要素だけを表示名として使う
    name = re.split(r"[/\\]", str(raw_name))[-1]

    # 制御文字（改行・タブ・NUL など）を取り除く
    name = _CONTROL_CHARS_PATTERN.sub("", name).strip()

    # 空になった場合や '.' '..' だけになった場合は表示名として使えない
    if not name or name in {".", ".."}:
        return fallback

    return name


def build_content_disposition(filename: str) -> str:
    """ダウンロード時の Content-Disposition ヘッダの値を組み立てて返す。

    入力:
        filename … ブラウザに提示したい表示名（_safe_download_name を通した値）

    出力:
        ヘッダに載せる文字列
        例: 'attachment; filename="report.pdf"'
            "attachment; filename*=utf-8''%E5%B0%B1%E6%A5%AD%E8%A6%8F%E5%89%87.txt"

    なぜ自前で組み立てるのか:
        以前はローカルファイルを FileResponse で返しており、このヘッダの組み立ては
        Starlette が担っていた。保存先がGCSに変わり StreamingResponse を使うため、
        同じ役割の処理をこちらに持つ必要がある。

    どうやって安全性を保つか（ヘッダインジェクション対策）:
        Starlette の FileResponse と同じ方式をそのまま使う。
        1. 表示名を urllib.parse.quote に通す
        2. エンコード結果が元と変わったなら、値に「そのままヘッダに置けない文字」が
           含まれていたということなので、RFC5987 形式（filename*=utf-8''...）で
           エンコード済みの文字列だけを載せる
        3. 変わらなかったなら、ASCIIの安全な文字だけの名前なので filename="..." で載せる

        quote は改行(\\r \\n)・ダブルクォート・非ASCIIをすべてパーセントエンコードする。
        つまり「ヘッダを分割する」「値を閉じて別の属性を差し込む」に使える文字は、
        必ず 2 の経路に入ってエンコードされ、生のままヘッダへは出られない。

        なお値が変わらない 3 の経路に入るのは、quote が素通しする文字
        （英数字と _.-~/ の一部）だけで構成された名前に限られる。
        '/' は _safe_download_name が先に落としているので、ここには届かない。
    """
    quoted = quote(filename)

    # エンコードで値が変わった＝そのままヘッダに置けない文字が含まれていた
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"

    return f'attachment; filename="{filename}"'


@router.get("/{source_id}/download")
async def download_source(source_id: int, token: dict = Depends(require_ceo)):
    """登録済みソースの実ファイルをダウンロードさせる（社長専用）。

    入力:
        source_id … ダウンロードしたいソースのID（URLパスで指定）
        token     … require_ceo が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        ファイルの中身（StreamingResponse）。ブラウザには元のファイル名で保存させる。

    エラー:
        403 … 社員がアクセスした場合（require_ceo が投げる）
        400 … URLソースを指定した場合（実ファイルが存在しないため）
        404 … ソースが無い / 保存パスが想定形式でない / 実体が見つからない

    処理:
        1. source_id でDBを引く（無ければ404）
        2. URLソースを弾く
        3. file_path が保存名の生成規則に合うか検証する
        4. 実体の存在を確認する
        5. ストレージから少しずつ読み出して返す

    なぜ署名付きURLではなくサーバー経由で配信するのか（権限ルール）:
        署名付きURLは、URLを持っている人ならアプリを通さずにファイルを取得できる。
        そうなると require_ceo の判定を通らずに個別ソース（他人の評価・給与など）を
        落とせてしまい、CLAUDE.md の権限ルールと噛み合わない。
        サーバー経由なら、毎回この関数の権限チェックを必ず通る。

    なぜ一括ではなく少しずつ返すのか:
        ファイルは最大50MB。全体をメモリに載せてから返すと、
        同時にダウンロードが重なったときにメモリを圧迫する。

    なぜURLソースを明示的に弾くのか:
        URLソースの file_path には、社長が入力したURL文字列がそのまま入っている
        （register_url を参照）。つまりこの列は「常にサーバー生成の安全な値」ではない。
        file_type='url' の行を先に落とすことで、ユーザー入力そのものが
        パス処理へ流れ込む経路を入口で断つ。

    なぜ file_path をそのまま渡さないのか（パストラバーサル対策）:
        DBの値であっても信用しない。ストレージ層が app/upload_paths.py の
        組み立て関数に通し、「コード側の定数」と「正規表現を通ったファイル名」だけから
        保存先の名前を作り直す。検証を通らない値（形式の違う古いデータなど）は
        ValueError になるので、その場合は404として扱う。
    """
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT file_name, file_type, file_path FROM sources WHERE source_id = %s",
            (source_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="ソースが見つかりません")

    # URLソースには実ファイルが存在しない（file_path にはURL文字列が入っている）
    if row["file_type"] == "url":
        raise HTTPException(status_code=400, detail="URLソースはダウンロードできません")

    # DBの値を信用せず、保存名の生成規則に合うかをここで確かめる。
    # 通った名前は、表示名が使えなかったときの代替名としても使う
    # storage.exists は同期関数で、GCS が保存先のときは中で通信する。
    # async のルーターから直接呼ぶと通信待ちの間イベントループが止まるため、別スレッドへ逃がす
    try:
        safe_name = sanitize_upload_name(row["file_path"])
        file_exists = await run_in_threadpool(storage.exists, row["file_path"])
    except ValueError:
        # 保存名の生成規則に合わない＝形式の違う古いデータや壊れた行。
        # 利用者から見れば「取り出せるファイルが無い」ので404で返す。
        # リクエスト由来の値は、改行を落としてからログに渡す（ログインジェクション対策）
        logger.warning(
            "保存パスが想定の形式ではありません source_id=%s", _sanitize_for_log(source_id)
        )
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")

    # DBに行はあるが実体が消えている場合（手動削除など）
    if not file_exists:
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")

    # ブラウザに提示する名前は元のファイル名。実際に読む先は storage が決めるので、
    # 「見せる名前」と「保存先の名前」は最後まで分離したままにする。
    # media_type も file_name からの推測ではなく file_type の定数表から引く
    # （表示名に拡張子が無い場合でも正しい種別で返すため）
    download_name = _safe_download_name(row["file_name"], safe_name)

    return StreamingResponse(
        storage.open_stream(row["file_path"]),
        media_type=MEDIA_TYPE_BY_TYPE.get(row["file_type"], "application/octet-stream"),
        headers={"content-disposition": build_content_disposition(download_name)},
    )


@router.delete("/{source_id}")
async def delete_source(source_id: int, token: dict = Depends(require_source_uploader)):
    """ソースを削除する（DB・実ファイル・Qdrantのベクトルをまとめて消す）。

    誰が使えるか（権限ルール）:
        入口の require_source_uploader で ceo / source_manager に絞る。
        そのうえで、
        - ceo（社長）… 共通ソースも個別ソースも削除できる
        - source_manager（共通ソース管理者）… 共通ソース（scope='common'）だけ削除できる。
          他人の個別ソース（評価・給与などの資料）には一切触れさせないため、
          個別ソースを指定された場合は403で弾く。
        社員（employee）は入口の require_source_uploader で403になる。

    処理:
        1. 対象のソースがあるか確認する（無ければ404）
        2. scope による権限判定（source_manager は共通ソースのみ）
        3. Qdrantから、そのソース由来のチャンクを削除する
        4. 保存した実体を削除する（URLの場合は実体が無いのでスキップ）
        5. sources テーブルから削除する

    なぜQdrantを先に消すのか:
        DBを先に消すと、途中で失敗したときに「DBには無いがQdrantには残る」状態になり、
        消したはずの資料をAIが参照し続ける。しかもDBから消えているので、
        どのsource_idを消せばいいか追跡できなくなる。
        逆にQdrantを先に消せば、途中で失敗してもDBに行が残るので、削除をやり直せる。
        実体の削除も同じ理由でDBより先に行う。

    なぜ file_path を直接 Path に渡さないのか（パストラバーサル対策）:
        以前はここだけ検証なしで Path(row["file_path"]) を作って unlink していた。
        削除は「消す」操作なので、想定外のパスが渡ると別のファイルを消しうる。
        ストレージ層を通すことで、読み出しと同じ検証（保存名の生成規則）を必ず経る。
    """
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT * FROM sources WHERE source_id = %s", (source_id,)).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="ソースが見つかりません")

    # Issue #118。共通ソース管理者（source_manager）は共通ソースだけ削除できる。
    # 社長（ceo）はこれまで通りすべて削除できる。
    #
    # なぜ source_id を受け取った時点で判定できないか:
    #     受け取れるのは source_id だけで、その時点では共通ソースか個別ソースか分からない。
    #     判定にはDBの scope が要る。404判定のために行は既に取得しているので、
    #     その直後に scope を見るのが、余計な問い合わせを増やさない一番早い位置になる。
    #
    # なぜエンドポイントを分けないか:
    #     この下の削除処理は Qdrant → 実体 → DB の順序と、その理由
    #     （途中で失敗してもDBに行が残り、削除をやり直して復旧できるようにする）を
    #     厳密に守る必要がある。同じ手順を2箇所に持つと、片方だけ直し忘れる事故が起きる。
    #     そこで削除処理は1つのまま、判定だけを入口に置く形にした。
    if token["role"] != ROLE_CEO and row["scope"] != "common":
        raise HTTPException(status_code=403, detail="このソースを削除する権限がありません")

    # Qdrantから、このソース由来のチャンクをすべて削除する
    # （消し残すと、削除済みの資料をAIが回答の根拠にし続けてしまう）
    try:
        delete_by_source_id(str(source_id))
    except Exception:
        # リクエスト由来の値は、改行を落としてからログに渡す（ログインジェクション対策）
        logger.exception(
            "Qdrantからの削除に失敗しました source_id=%s", _sanitize_for_log(source_id)
        )
        raise HTTPException(
            status_code=500,
            detail="ベクトルの削除に失敗しました。ソースは削除されていません",
        )

    # ファイルの場合は保存した実体も削除する（URLは実体が無いのでスキップ）
    if row["file_type"] != "url":
        # storage.delete は同期関数で、GCS が保存先のときは中で通信する。
        # async のルーターから直接呼ぶと通信待ちの間イベントループが止まるため、別スレッドへ逃がす
        try:
            await run_in_threadpool(storage.delete, row["file_path"])
        except ValueError:
            # 保存名の生成規則に合わない＝形式の違う古いデータや壊れた行。
            # 何を消せばよいか特定できないので、実体の削除は諦めてDBの行だけ消す。
            # ここで止めてしまうと、その行を二度と削除できなくなる
            logger.warning(
                "保存パスが想定の形式ではないため実体の削除をスキップします source_id=%s",
                _sanitize_for_log(source_id),
            )
        except Exception:
            # 通信エラーなど。DBの行を残して再実行できるようにする
            # （行を消してしまうと、どの実体が残っているか追跡できなくなる）
            logger.exception(
                "ファイルの削除に失敗しました source_id=%s", _sanitize_for_log(source_id)
            )
            raise HTTPException(
                status_code=500,
                detail="ファイルの削除に失敗しました。ソースは削除されていません",
            )

    # 最後にDBの行を削除する
    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM sources WHERE source_id = %s", (source_id,))
        conn.commit()

    return {"success": True, "source_id": source_id}
