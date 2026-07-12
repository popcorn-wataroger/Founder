import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.database import get_connection
from app.routers.auth_router import verify_token
from app.vector_store import delete_by_source_id, ensure_collection, save_chunks
from app.vectorizer import embed_text, extract_text, split_into_chunks

router = APIRouter(prefix="/api/sources", tags=["sources"])

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("uploads")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".txt"}


def require_admin(token: dict = Depends(verify_token)) -> dict:
    """管理者以外は403を返す"""
    if token.get("role") != "admin":
        raise HTTPException(status_code=403, detail="管理者のみ操作できます")
    return token


@router.get("")
async def list_sources(token: dict = Depends(require_admin)):
    """ソース一覧を返す"""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM sources ORDER BY uploaded_at DESC").fetchall()
    conn.close()
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


def _rollback_source(source_id: int, save_path: Path | None) -> None:
    """ベクトル化に失敗したとき、DB登録とファイル保存を取り消す。

    入力:
        source_id … 取り消したいソースのID
        save_path … 保存したファイルのパス（URL登録などファイルが無い場合は None）

    出力:
        なし

    なぜロールバックするか:
        ベクトル化に失敗したままDBに行を残すと、
        「一覧には出るのにAIが中身を知らないソース」ができてしまう。
        社長から見ると登録できたように見えるのに、誰が質問しても答えられない。
        中途半端な状態を残さないよう、失敗時は登録前の状態まで戻す。
    """
    # DBの登録を取り消す
    conn = get_connection()
    conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
    conn.commit()
    conn.close()

    # 保存した実ファイルも消す（ファイルが無い登録＝URLの場合はスキップ）
    if save_path is not None and save_path.exists():
        save_path.unlink()


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
    token: dict = Depends(require_admin),
):
    """ファイルをアップロードしてソースとして登録し、AIが検索できるようベクトル化する。

    処理:
        1. 入力チェック（scope、ファイル名、拡張子、サイズ）
        2. uploads/ にファイルを保存する
        3. sources テーブルに登録し、source_id を採番する
        4. 本文を取り出してベクトル化し、Qdrantに保存する
        5. 4が失敗したら、3のDB登録と2のファイル保存を取り消して（ロールバック）エラーを返す

    なぜ順番が「DB登録 → ベクトル化」なのか:
        Qdrantに保存するとき、どのソース由来かを示す source_id が必要になる。
        source_id はDBに登録して初めて採番される（AUTOINCREMENT）ため、先にDB登録する。
    """
    validate_scope(scope, owner_user_id)
    if not file.filename:
        raise HTTPException(status_code=400, detail="ファイル名が取得できません")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"対応していないファイル形式です。対応形式: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="ファイルサイズが50MBを超えています")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 同名ファイルの衝突を避けるためタイムスタンプをプレフィックスに付ける
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    save_name = f"{timestamp}_{file.filename}"
    save_path = UPLOAD_DIR / save_name
    save_path.write_bytes(contents)

    uploaded_at = datetime.now(timezone.utc).isoformat()
    file_type = suffix.lstrip(".")

    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO sources
            (file_name, file_type, file_path, scope, owner_user_id, uploaded_at, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file.filename,
            file_type,
            str(save_path),
            scope,
            owner_user_id,
            uploaded_at,
            token["user_id"],
        ),
    )
    conn.commit()
    source_id = cursor.lastrowid
    conn.close()

    # 本文を取り出してベクトル化し、Qdrantに保存する（ここまで成功して初めて「登録完了」）
    try:
        chunk_count = _vectorize_and_save(
            source_id=source_id,
            path=str(save_path),
            file_type=file_type,
            scope=scope,
            owner_user_id=owner_user_id,
        )
    except Exception:
        # 失敗したら、直前のDB登録とファイル保存を取り消して登録前の状態に戻す
        logger.exception("ベクトル化に失敗しました。ソース登録を取り消します source_id=%s", source_id)
        _rollback_source(source_id, save_path)
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
async def register_url(req: UrlRequest, token: dict = Depends(require_admin)):
    """URLをソースとして登録する"""
    validate_scope(req.scope, req.owner_user_id)
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="有効なURLを入力してください")

    display_name = req.file_name or req.url
    uploaded_at = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO sources
            (file_name, file_type, file_path, scope, owner_user_id, uploaded_at, uploaded_by)
        VALUES (?, 'url', ?, ?, ?, ?, ?)
        """,
        (display_name, req.url, req.scope, req.owner_user_id, uploaded_at, token["user_id"]),
    )
    conn.commit()
    source_id = cursor.lastrowid
    conn.close()

    return {"success": True, "source_id": source_id, "url": req.url}


@router.delete("/{source_id}")
async def delete_source(source_id: int, token: dict = Depends(require_admin)):
    """ソースを削除する（DB・実ファイル・Qdrantのベクトルをまとめて消す）。

    処理:
        1. 対象のソースがあるか確認する（無ければ404）
        2. Qdrantから、そのソース由来のチャンクを削除する
        3. 実ファイルを削除する（URLの場合はファイルが無いのでスキップ）
        4. sources テーブルから削除する

    なぜQdrantを先に消すのか:
        DBを先に消すと、途中で失敗したときに「DBには無いがQdrantには残る」状態になり、
        消したはずの資料をAIが参照し続ける。しかもDBから消えているので、
        どのsource_idを消せばいいか追跡できなくなる。
        逆にQdrantを先に消せば、途中で失敗してもDBに行が残るので、削除をやり直せる。
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()

    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="ソースが見つかりません")

    conn.close()

    # Qdrantから、このソース由来のチャンクをすべて削除する
    # （消し残すと、削除済みの資料をAIが回答の根拠にし続けてしまう）
    try:
        delete_by_source_id(str(source_id))
    except Exception:
        logger.exception("Qdrantからの削除に失敗しました source_id=%s", source_id)
        raise HTTPException(
            status_code=500,
            detail="ベクトルの削除に失敗しました。ソースは削除されていません",
        )

    # ファイルの場合は実ファイルも削除する
    if row["file_type"] != "url":
        file_path = Path(row["file_path"])
        if file_path.exists():
            file_path.unlink()

    # 最後にDBの行を削除する
    conn = get_connection()
    conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
    conn.commit()
    conn.close()

    return {"success": True, "source_id": source_id}
