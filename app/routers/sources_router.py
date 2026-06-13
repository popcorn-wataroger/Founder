from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.database import get_connection
from app.routers.auth_router import verify_token

router = APIRouter(prefix="/api/sources", tags=["sources"])

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


@router.post("/upload")
async def upload_source(
    file: UploadFile,
    scope: str = Form("common"),
    owner_user_id: str | None = Form(None),
    token: dict = Depends(require_admin),
):
    """ファイルをアップロードしてソースとして登録する"""
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

    return {"success": True, "source_id": source_id, "file_name": file.filename}


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
    """ソースを削除する"""
    conn = get_connection()
    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()

    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="ソースが見つかりません")

    # ファイルの場合は実ファイルも削除する
    if row["file_type"] != "url":
        file_path = Path(row["file_path"])
        if file_path.exists():
            file_path.unlink()

    conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
    conn.commit()
    conn.close()

    return {"success": True, "source_id": source_id}
