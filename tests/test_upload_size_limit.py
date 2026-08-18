"""アップロードのサイズ検証が「逐次読み込み」で行われることのテスト（Issue #98）。

対象:
    app/routers/sources_router.py の read_upload_within_limit()
    POST /api/sources/upload      … 共通ソースのアップロード
    POST /api/sources/my-upload   … 社員が自分の個別ソースをアップロード

何を守りたいか:
    1. 上限を超えるファイルは、全体をメモリに載せる前に413で拒否されること
    2. 2つのエンドポイントが同じ検証ロジック（read_upload_within_limit）を通ること
    3. 上限内のファイルは従来どおり登録できること

なぜ MAX_FILE_SIZE を差し替えるか:
    本物の上限は50MB。それを超えるファイルをテストで作ると、
    実行のたびに50MB超のデータを生成することになり、遅くなる。
    確かめたいのは「上限を超えたら打ち切る」という仕組みであって、
    上限の値が50MBであることではないので、小さい値に差し替えて検証する。
"""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import storage, upload_paths
from app.main import app
from app.routers import sources_router
from app.routers.auth_router import ROLE_ADMIN, ROLE_EMPLOYEE, create_access_token

# users.csv 上の ADMIN（社長）と EMP001（社員）の user_id
ADMIN_USER_ID = "1"
EMPLOYEE_USER_ID = "2"

# テスト用に差し替える上限（10バイト）と、それを超える本文
TEST_MAX_FILE_SIZE = 10
OVERSIZED_CONTENT = b"a" * (TEST_MAX_FILE_SIZE + 1)


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。"""
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: None) -> Iterator[Path]:
    """本番の uploads/ を汚さないよう、使い捨ての置き場に差し替える。

    tests/test_my_source_upload.py の同名フィクスチャと同じ考え方。
    ベクトル化（Gemini・Qdrantへの実通信）も差し替える。
    """
    test_upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(upload_paths, "UPLOAD_DIR", test_upload_dir)
    monkeypatch.setattr(storage, "GCS_BUCKET_NAME", "")
    monkeypatch.setattr(storage, "APP_ENV", "test")

    def fake_vectorize_and_save(**kwargs: object) -> int:
        return 1

    monkeypatch.setattr(sources_router, "_vectorize_and_save", fake_vectorize_and_save)

    yield test_upload_dir


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限を10バイトに、読み込み単位を1バイトに差し替える。

    読み込み単位も小さくする理由:
        1MBずつ読む設定のままだと、10バイトのファイルは1回の read() で
        全部読み終えてしまい、「途中で打ち切った」ことにならない。
        1バイトずつにすれば、超過した時点で残りが未読のまま止まることを確かめられる。
    """
    monkeypatch.setattr(sources_router, "MAX_FILE_SIZE", TEST_MAX_FILE_SIZE)
    monkeypatch.setattr(sources_router, "UPLOAD_CHUNK_SIZE", 1)


def _upload_common(content: bytes) -> httpx.Response:
    """共通ソースとしてファイルを登録するAPIを叩く（テスト用の短縮形）。"""
    return TestClient(app).post(
        "/api/sources/upload",
        files={"file": ("就業規則.txt", content, "text/plain")},
        data={"scope": "common"},
        headers=_headers(ADMIN_USER_ID, ROLE_ADMIN),
    )


def _upload_my(content: bytes) -> httpx.Response:
    """自分の個別ソースとしてファイルを登録するAPIを叩く（テスト用の短縮形）。"""
    return TestClient(app).post(
        "/api/sources/my-upload",
        files={"file": ("私の資格証明.txt", content, "text/plain")},
        headers=_headers(EMPLOYEE_USER_ID, ROLE_EMPLOYEE),
    )


# ===== ヘルパー単体のテスト =====


@pytest.mark.anyio
async def test_上限を超えた時点で読み込みを止める(small_limit: None) -> None:
    """超過した時点で打ち切るので、残りのデータは読まれない。

    何を確かめているか:
        read() が呼ばれた回数を数える。全体を読み切ってから判定する実装なら
        最後まで読み進めてしまうが、打ち切る実装なら上限を1つ超えた時点で止まる。
        「拒否する分はメモリに載せない」という Issue #98 の目的が、
        呼び出し回数という観測できる形で確認できる。
    """
    read_sizes: list[int] = []
    remaining = bytearray(b"a" * 100)

    class FakeUploadFile:
        async def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            chunk = bytes(remaining[:size])
            del remaining[:size]
            return chunk

    with pytest.raises(sources_router.HTTPException) as exc_info:
        await sources_router.read_upload_within_limit(FakeUploadFile())  # type: ignore[arg-type]

    assert exc_info.value.status_code == 413
    # 1バイトずつ読み、上限(10)を1つ超えた11回目で止まる
    assert len(read_sizes) == TEST_MAX_FILE_SIZE + 1
    # 残りは未読のまま（100 - 11 = 89バイト）
    assert len(remaining) == 89


@pytest.mark.anyio
async def test_上限内なら全体を返す(small_limit: None) -> None:
    """上限以内のファイルは、これまでどおり全体が読み込まれる。"""
    content = b"a" * TEST_MAX_FILE_SIZE
    remaining = bytearray(content)

    class FakeUploadFile:
        async def read(self, size: int = -1) -> bytes:
            chunk = bytes(remaining[:size])
            del remaining[:size]
            return chunk

    result = await sources_router.read_upload_within_limit(FakeUploadFile())  # type: ignore[arg-type]

    assert result == content


# ===== エンドポイント経由のテスト（両方が同じ検証を通ること）=====


def test_共通ソースは上限超過で413(upload_dir: Path, small_limit: None) -> None:
    """POST /api/sources/upload が上限超過を413で拒否する。"""
    response = _upload_common(OVERSIZED_CONTENT)

    assert response.status_code == 413, response.text


def test_個別ソースは上限超過で413(upload_dir: Path, small_limit: None) -> None:
    """POST /api/sources/my-upload も同じ検証を通る。

    なぜ両方を確かめるか:
        入口によって通るサイズが違う状態を作らないため。
        片方だけ直すと、同じファイルが片方では登録でき、
        もう片方では拒否されるという説明のつかない挙動になる。
    """
    response = _upload_my(OVERSIZED_CONTENT)

    assert response.status_code == 413, response.text


def test_上限内の共通ソースは従来どおり登録できる(upload_dir: Path, small_limit: None) -> None:
    """上限を下げても、その範囲内なら登録は成功する（既存の挙動を壊していない）。"""
    response = _upload_common(b"a" * TEST_MAX_FILE_SIZE)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


def test_上限内の個別ソースは従来どおり登録できる(upload_dir: Path, small_limit: None) -> None:
    """my-upload 側も上限内なら登録できる。"""
    response = _upload_my(b"a" * TEST_MAX_FILE_SIZE)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
