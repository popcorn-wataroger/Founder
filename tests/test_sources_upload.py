"""ファイルアップロード（POST /api/sources/upload）のパストラバーサル対策のテスト。

何を守りたいか:
    file.filename はクライアントが自由に決められる。'../../evil.txt' や
    '/etc/evil.txt' のような値を送られても、uploads/ の外に書き込ませない。

なぜ本物のエンドポイントを叩くか:
    保存パスの組み立ては upload_source の中にある。テスト側で同じロジックを
    書き写して検証しても、本番コードを直し忘れたときに気づけない。
    そこで TestClient で実際にエンドポイントを通し、外部依存（認証・Gemini・
    Qdrant・本番のuploads/とDB）だけを差し替える。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.routers import sources_router
from app.routers.auth_router import require_admin


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """本番の uploads/ と data/founder.db を汚さないよう、使い捨ての置き場に差し替える。

    出力:
        テスト用の uploads ディレクトリのパス（この中にだけ保存されるはず）
    """
    # 保存先を tmp_path 配下に向ける
    test_upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(sources_router, "UPLOAD_DIR", test_upload_dir)

    # DBも使い捨てのファイルに向け、テーブルを作っておく
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()

    # ベクトル化は本物だと Gemini と Qdrant に実通信してしまうので差し替える
    # （今回検証したいのは「どこに保存されるか」であって、ベクトル化の中身ではない）
    def fake_vectorize_and_save(**kwargs: object) -> int:
        return 1

    monkeypatch.setattr(sources_router, "_vectorize_and_save", fake_vectorize_and_save)

    # 管理者チェックを通す（今回検証したいのは認証ではないため、ADMIN でログイン済みとみなす）
    app.dependency_overrides[require_admin] = lambda: {"user_id": "ADMIN"}

    yield test_upload_dir

    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _upload(client: TestClient, file_name: str) -> object:
    """指定したファイル名で /api/sources/upload を叩く。"""
    return client.post(
        "/api/sources/upload",
        files={"file": (file_name, "これはテスト用の本文です".encode(), "text/plain")},
        data={"scope": "common"},
    )


def _saved_file_name(source_id: int) -> str:
    """DBに記録された「表示用のファイル名」を取り出す。"""
    conn = database.get_connection()
    row = conn.execute(
        "SELECT file_name, file_path FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    conn.close()
    return row


@pytest.mark.parametrize(
    "危険なファイル名",
    [
        "../../evil.txt",  # 相対パスで上の階層へ抜ける
        "../../../etc/evil.txt",
        "/etc/evil.txt",  # 絶対パス（Path の / は左辺を捨ててしまう）
        "..\\..\\evil.txt",  # Windows形式の区切り文字
        "subdir/../../evil.txt",
    ],
)
def test_危険なファイル名でもuploads配下から出ない(
    client: TestClient, upload_dir: Path, tmp_path: Path, 危険なファイル名: str
) -> None:
    """パストラバーサルを狙ったファイル名を送っても uploads/ の外に書き込まれない。

    許容する結果は2つだけ:
        1. 400 で弾かれる
        2. 保存はされるが、保存先が必ず uploads/ の配下に収まっている
    どちらの場合も「uploads/ の外にファイルが増えていない」ことを必ず確かめる。
    """
    response = _upload(client, 危険なファイル名)

    # uploads/ の外（tmp_path直下や親）にファイルが漏れ出していないこと
    漏れたファイル = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and not p.is_relative_to(upload_dir) and p.suffix != ".db"
    ]
    assert 漏れたファイル == [], f"uploads/ の外にファイルが作られた: {漏れたファイル}"

    if response.status_code == 400:
        return  # 弾かれた（これも正解）

    assert response.status_code == 200, response.text
    source_id = response.json()["source_id"]

    # 保存先が本当に uploads/ の中か、DBに記録されたパスで確認する
    row = _saved_file_name(source_id)
    保存パス = Path(row["file_path"]).resolve()
    assert 保存パス.is_relative_to(upload_dir.resolve())

    # ディスク上の名前にユーザーの入力が一切混ざっていないこと
    assert ".." not in 保存パス.name
    assert "evil" not in 保存パス.name


def test_正常なファイル名は保存でき元の名前がDBに残る(client: TestClient, upload_dir: Path) -> None:
    """正常系。ディスク上は安全な名前、DBの file_name は元の名前、という分離を固定する。"""
    response = _upload(client, "就業規則.txt")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True

    row = _saved_file_name(body["source_id"])

    # 表示用の名前は元のファイル名のまま（画面にはこれが出る）
    assert row["file_name"] == "就業規則.txt"

    # ディスク上の名前は安全な自動生成名。拡張子だけ引き継ぐ
    保存パス = Path(row["file_path"])
    assert 保存パス.is_relative_to(upload_dir)
    assert 保存パス.suffix == ".txt"
    assert "就業規則" not in 保存パス.name
    assert 保存パス.exists()  # 実ファイルがちゃんと書けている
