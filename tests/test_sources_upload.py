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
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import database, storage, upload_paths
from app.main import app
from app.routers import sources_router
from app.routers.auth_router import require_admin


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: None) -> Iterator[Path]:
    """本番の uploads/ を汚さないよう、使い捨ての置き場に差し替える。

    出力:
        テスト用の uploads ディレクトリのパス（この中にだけ保存されるはず）

    補足:
        保存先の判断は app/storage.py が持っているので、そこを
        「バケット未設定 かつ テスト環境」に固定してローカル保存の経路に倒す。
        実際のパスを組み立てるのは app/upload_paths.py の UPLOAD_DIR なので、
        向け直すのはそちら（storage は自分では持っていない）。

        DBの用意は共通フィクスチャ temp_db に任せている
        （テスト用PostgreSQLのテーブルを空にしてから始める）。
    """
    # 保存先を tmp_path 配下に向ける
    test_upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(upload_paths, "UPLOAD_DIR", test_upload_dir)
    monkeypatch.setattr(storage, "GCS_BUCKET_NAME", "")
    monkeypatch.setattr(storage, "APP_ENV", "test")

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


def _upload(client: TestClient, file_name: str) -> httpx.Response:
    """指定したファイル名で /api/sources/upload を叩く。"""
    return client.post(
        "/api/sources/upload",
        files={"file": (file_name, "これはテスト用の本文です".encode(), "text/plain")},
        data={"scope": "common"},
    )


def _saved_file_name(source_id: int) -> dict[str, Any]:
    """DBに記録された file_name（表示用の名前）と file_path（保存先）の行を取り出す。"""
    conn = database.get_connection()
    row = conn.execute(
        "SELECT file_name, file_path FROM sources WHERE source_id = %s", (source_id,)
    ).fetchone()
    conn.close()
    # 呼び出し元は登録済みの source_id しか渡さない。無ければテストの前提が崩れている
    assert row is not None, f"source_id={source_id} の行がDBに存在しない"
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
        p for p in tmp_path.rglob("*") if p.is_file() and not p.is_relative_to(upload_dir)
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


def test_DB登録に失敗したら保存済みファイルが残らない(
    client: TestClient, upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB登録が失敗したとき、先に保存したファイルが消えることを確かめる。

    なぜこのテストが要るか:
        upload_source は「ファイル保存 → DB登録 → ベクトル化」の順で進む。
        DB登録が失敗すると、保存済みのファイルだけがストレージに残る。
        DBに行が無いので画面にも出ず、削除する手段も無い孤児ファイルになる。

    どうやってDB登録だけを失敗させるか:
        sources_router が使う get_connection を、呼ぶと必ず例外を投げる関数に
        差し替える。ファイル保存（storage.save）はその前に終わっているので、
        「保存は成功したがDB登録で落ちた」状況をそのまま再現できる。

    確かめること:
        1. 例外が外へ伝わる（後始末が元の原因を握りつぶしていない）
        2. uploads/ にファイルが1つも残っていない
    """

    def fail_get_connection() -> None:
        raise RuntimeError("DB接続に失敗しました（テスト用）")

    monkeypatch.setattr(sources_router, "get_connection", fail_get_connection)

    # 後始末が元の例外を上書きしていないこと。
    # DB接続の失敗がそのまま外へ出てくるのが正しい
    with pytest.raises(RuntimeError, match="DB接続に失敗しました（テスト用）"):
        _upload(client, "就業規則.txt")

    # 保存済みファイルが消えていること（孤児ファイルが残らない）
    残ったファイル = [p for p in upload_dir.rglob("*") if p.is_file()]
    assert 残ったファイル == [], f"DB登録に失敗したのにファイルが残っている: {残ったファイル}"
