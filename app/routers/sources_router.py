import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import UPLOAD_DIR
from app.database import get_connection
from app.routers.auth_router import require_admin
from app.upload_paths import build_safe_upload_path
from app.vector_store import delete_by_source_id, ensure_collection, save_chunks
from app.vectorizer import embed_text, extract_text, split_into_chunks

router = APIRouter(prefix="/api/sources", tags=["sources"])

# 管理者専用のソースAPIをまとめるルーター。URLは /api/admin から始まる
# （社長が社員データ画面で、その社員の個別ソースだけを見るために使う）
admin_router = APIRouter(prefix="/api/admin", tags=["sources"])

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".txt"}

# ソース種別 → 保存に使う拡張子の対応表。
# なぜ辞書で持つか（パストラバーサル対策）:
#     保存パスに載せる拡張子は、ユーザーのファイル名から切り出した文字列ではなく
#     「コード側が持つ定数」から引く。こうすると保存パスを構成する文字列に
#     ユーザー入力が1文字も混ざらない。
#     入口の ALLOWED_EXTENSIONS チェック（値を変えない検査）だけでは、
#     静的解析から見て「ユーザー入力がパスまで届いている」状態が続いてしまうため、
#     値そのものを定数に置き換えて汚染を断ち切る。
EXTENSION_BY_TYPE = {"pdf": ".pdf", "docx": ".docx", "pptx": ".pptx", "txt": ".txt"}

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
async def list_sources(token: dict = Depends(require_admin)):
    """ソース一覧を返す"""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM sources ORDER BY uploaded_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


@admin_router.get("/users/{user_id}/sources")
async def list_user_sources(
    user_id: str, token: dict = Depends(require_admin)
) -> list[dict[str, Any]]:
    """指定した社員の「個別ソース」だけを一覧で返す（社長専用）。

    入力:
        user_id … ソースを見たい社員の user_id（URLパスで指定）
        token   … require_admin が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        その社員の個別ソースの一覧（登録日の新しい順）。
        各件に source_id / file_name / file_type / scope / owner_user_id / uploaded_at を含む

    使いどころ:
        社員データ画面の「過去のソース」欄。その社員に紐づく資料だけを並べる。

    絞り込み条件について（権限ルール）:
        WHERE に scope='individual' と owner_user_id=? の両方を必ず付ける。
        - owner_user_id だけで絞ると、共通ソースは owner_user_id が NULL なので
          混ざりはしないが、条件が「持ち主」だけになり意図が読み取りづらい
        - scope だけで絞ると、他人の個別ソースまで出てしまう（絶対に不可）
        この欄は「その社員の資料」を並べる場所なので、全社共通ソースも出さない。

    file_path を返さない理由:
        file_path はサーバー内部の保存先パスで、画面には不要。
        外に出すと保存場所の構造が分かってしまうため、返す列を明示的に選んでいる。
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT source_id, file_name, file_type, scope, owner_user_id, uploaded_at
        FROM sources
        WHERE scope = 'individual' AND owner_user_id = ?
        ORDER BY uploaded_at DESC
        """,
        (user_id,),
    ).fetchall()
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

    # ソース種別（pdf / docx / pptx / txt）。上のチェックを通っているので必ず辞書に存在する
    file_type = suffix.lstrip(".")

    # ディスク上の保存名は、ユーザーのファイル名を一切使わずサーバー側で組み立てる
    # なぜ必要か（パストラバーサル対策）:
    #     file.filename はクライアントが自由に決められる文字列で、
    #     '../../app/main.py' や '/etc/cron.d/evil' のような値も送りつけられる。
    #     これをそのまま連結すると uploads/ の外へ書き込めてしまう
    #     （Path の / は「安全な結合」ではなく単なる連結で、右が絶対パスなら左を捨てる）。
    # timestamp   … 人が見て「いつの投入か」を追えるように
    # uuid4       … 同時アップロードでも名前が衝突しないように
    # safe_suffix … ユーザーのファイル名から切り出した文字列ではなく、
    #               EXTENSION_BY_TYPE が持つ定数を使う。これで保存パスを組み立てる
    #               材料が全てコード側の値になり、ユーザー入力が1文字も混ざらない
    safe_suffix = EXTENSION_BY_TYPE[file_type]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    save_name = f"{timestamp}_{uuid.uuid4()}{safe_suffix}"
    save_path = UPLOAD_DIR / save_name

    # 書き込む直前に「本当に uploads/ の中か」を最終確認する（多層防御）
    # なぜ必要か（パストラバーサル対策）:
    #     上の生成方法なら理屈上は外に出ないが、将来ここのコードが変わったときに
    #     気づかず穴が開くのを防ぐ。resolve() で '..' やシンボリックリンクを解決した
    #     実体パスに直したうえで、uploads/ の配下にあることを確かめる。
    if not save_path.resolve().is_relative_to(UPLOAD_DIR.resolve()):
        raise HTTPException(status_code=400, detail="不正なファイル名です")

    save_path.write_bytes(contents)

    uploaded_at = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO sources
            (file_name, file_type, file_path, scope, owner_user_id, uploaded_at, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            # file_name … 画面に表示する「元のファイル名」。表示専用で、パスの組み立てには使わない
            # file_path … 実際の保存先。上で生成した安全な名前（uploads/ の中）
            # 「見せる名前」と「ディスク上の名前」を分けることでパストラバーサルを断つ
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

    # INSERT が成功していれば lastrowid には必ず値が入る。
    # None なら採番できていない＝本来ありえない状態なので、ここで止める
    if source_id is None:
        raise RuntimeError(
            f"sources への INSERT で source_id が採番されませんでした file_name={file.filename}"
        )

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
        logger.exception(
            "ベクトル化に失敗しました。ソース登録を取り消します source_id=%s", source_id
        )
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
    """URLをソースとして登録し、AIが検索できるようベクトル化する。

    処理:
        1. 入力チェック（scope、URLの形式）
        2. sources テーブルに登録し、source_id を採番する
        3. URLのページから本文を取り出してベクトル化し、Qdrantに保存する
        4. 3が失敗したら、2のDB登録を取り消して（ロールバック）エラーを返す

    ファイルアップロードとの違い:
        URLの場合は実ファイルを保存しないので、file_path にはURLをそのまま入れる。
        ロールバック時も消すファイルが無いため、_rollback_source に save_path=None を渡す。
        本文の取得は extract_text が file_type='url' としてページを取りに行く。
    """
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

    # INSERT が成功していれば lastrowid には必ず値が入る。
    # None なら採番できていない＝本来ありえない状態なので、ここで止める
    if source_id is None:
        raise RuntimeError(f"sources への INSERT で source_id が採番されませんでした url={req.url}")

    # URLのページ本文を取り出してベクトル化し、Qdrantに保存する
    # （ここまで成功して初めて「登録完了」。ファイルアップロードと同じ扱い）
    try:
        chunk_count = _vectorize_and_save(
            source_id=source_id,
            path=req.url,  # URLの場合、本文の取得元はURLそのもの
            file_type="url",
            scope=req.scope,
            owner_user_id=req.owner_user_id,
        )
    except Exception:
        # 失敗したら、直前のDB登録を取り消して登録前の状態に戻す
        # URLは実ファイルを保存していないので save_path は None
        logger.exception("ベクトル化に失敗しました。URL登録を取り消します source_id=%s", source_id)
        _rollback_source(source_id, None)
        raise HTTPException(
            status_code=500,
            detail="URLの読み取りまたはベクトル化に失敗しました。ソースは登録されていません",
        )

    return {
        "success": True,
        "source_id": source_id,
        "url": req.url,
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

        Starlette の FileResponse は filename を urllib.parse.quote に通し、
        エンコード結果が元と変わる場合は RFC5987 形式（filename*=utf-8''...）に
        切り替える。改行・ダブルクォート・非ASCIIはすべてパーセントエンコードされるため、
        ヘッダインジェクション自体はライブラリ側で防がれている（確認済み）。

        ただし '/' は quote の既定で素通りするため、'a/b.pdf' のような値は
        filename="a/b.pdf" とディレクトリ区切りを含んだまま提示されてしまう。
        表示名として不適切なので、ここで区切り文字と制御文字を落としておく。
        ライブラリの実装に安全性を全面的に預けない、という意味の多層防御でもある。

    注意:
        この関数が返すのは「見せる名前」だけで、実際に読むファイルのパスとは無関係。
        パスは build_safe_upload_path が別に組み立てる。
    """
    # '/' と '\' の両方を区切りとみなし、最後の要素だけを表示名として使う
    name = re.split(r"[/\\]", str(raw_name))[-1]

    # 制御文字（改行・タブ・NUL など）を取り除く
    name = _CONTROL_CHARS_PATTERN.sub("", name).strip()

    # 空になった場合や '.' '..' だけになった場合は表示名として使えない
    if not name or name in {".", ".."}:
        return fallback

    return name


@router.get("/{source_id}/download")
async def download_source(source_id: int, token: dict = Depends(require_admin)):
    """登録済みソースの実ファイルをダウンロードさせる（社長専用）。

    入力:
        source_id … ダウンロードしたいソースのID（URLパスで指定）
        token     … require_admin が返すログイン情報（社長でなければ403で弾かれている）

    出力:
        ファイルの中身（FileResponse）。ブラウザには元のファイル名で保存させる。

    エラー:
        403 … 社員がアクセスした場合（require_admin が投げる）
        400 … URLソースを指定した場合（実ファイルが存在しないため）
        404 … ソースが無い / 保存パスが想定形式でない / 実ファイルが見つからない

    処理:
        1. source_id でDBを引く（無ければ404）
        2. URLソースを弾く
        3. file_path から安全なパスを組み立て直す
        4. 実ファイルの存在を確認する
        5. FileResponse で返す

    なぜURLソースを明示的に弾くのか:
        URLソースの file_path には、社長が入力したURL文字列がそのまま入っている
        （register_url を参照）。つまりこの列は「常にサーバー生成の安全な値」ではない。
        file_type='url' の行を先に落とすことで、ユーザー入力そのものが
        パス処理へ流れ込む経路を入口で断つ。

    なぜ file_path をそのまま開かないのか（パストラバーサル対策）:
        DBの値であっても信用しない。build_safe_upload_path に通し、
        「コード側の定数 UPLOAD_DIR」と「正規表現を通ったファイル名」だけから
        パスを作り直す。検証を通らない値（形式の違う古いデータなど）は
        ValueError になるので、その場合は404として扱う。
        なお delete_source は現状 file_path を直接 Path に渡しているが、
        そちらの整理は本Issueの対象外（別Issueで扱う）。
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT file_name, file_type, file_path FROM sources WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="ソースが見つかりません")

    # URLソースには実ファイルが存在しない（file_path にはURL文字列が入っている）
    if row["file_type"] == "url":
        raise HTTPException(status_code=400, detail="URLソースはダウンロードできません")

    # DBの値を信用せず、検証を通った要素だけからパスを組み立て直す
    try:
        safe_path = build_safe_upload_path(row["file_path"])
    except ValueError:
        # 保存名の生成規則に合わない＝形式の違う古いデータや壊れた行。
        # 利用者から見れば「取り出せるファイルが無い」ので404で返す。
        # リクエスト由来の値は、改行を落としてからログに渡す（ログインジェクション対策）
        logger.warning(
            "保存パスが想定の形式ではありません source_id=%s", _sanitize_for_log(source_id)
        )
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")

    # DBに行はあるが実ファイルが消えている場合（手動削除など）
    if not safe_path.is_file():
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")

    # ブラウザに提示する名前は元のファイル名。実際に読むパスは safe_path で、
    # 「見せる名前」と「ディスク上の名前」は最後まで分離したままにする。
    # media_type も file_name からの推測ではなく file_type の定数表から引く
    # （表示名に拡張子が無い場合でも正しい種別で返すため）
    return FileResponse(
        path=safe_path,
        media_type=MEDIA_TYPE_BY_TYPE.get(row["file_type"], "application/octet-stream"),
        filename=_safe_download_name(row["file_name"], safe_path.name),
    )


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
        # リクエスト由来の値は、改行を落としてからログに渡す（ログインジェクション対策）
        logger.exception(
            "Qdrantからの削除に失敗しました source_id=%s", _sanitize_for_log(source_id)
        )
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
