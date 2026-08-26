"""ソースのダウンロード（GET /api/sources/{source_id}/download）のテスト。

何を守りたいか:
    1. 社員はダウンロードできない（管理者専用）
    2. DBの file_path を信用せず、uploads/ の外のファイルを読み出させない
    3. URLソースは実ファイルが無いので明示的に弾く
    4. file_name（完全なユーザー入力）をヘッダに載せてもインジェクションが起きない

4について（保存先がGCSになったことによる変更）:
    以前は Starlette の FileResponse が Content-Disposition を組み立てており、
    エンコードもライブラリ側が担っていた。GCSからのストリーミング配信に変えたため、
    同じ方式を build_content_disposition として自前に持っている。
    エンドポイント経由のテストに加えて、この関数単体にも同じ入力を通し、
    ライブラリが守ってくれていた性質を自分たちで担保していることを固定する。

なぜ本物のエンドポイントを叩くか:
    パスの組み立てと権限判定は download_source の中にある。テスト側で同じロジックを
    書き写しても、本番コードを直し忘れたときに気づけない。そこで TestClient で
    実際にエンドポイントを通し、外部依存（認証・本番のuploads/とDB）だけを差し替える。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, storage, upload_paths
from app.main import app
from app.routers.auth_router import require_ceo, verify_token
from app.routers.sources_router import _safe_download_name, build_content_disposition


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: None) -> Iterator[Path]:
    """本番の uploads/ を汚さないよう、使い捨ての置き場に差し替える。

    出力:
        テスト用の uploads ディレクトリのパス

    補足:
        差し替える UPLOAD_DIR は app.upload_paths のもの。
        安全なパスを組み立てるのは build_safe_upload_path であり、
        その関数が参照するモジュール変数を向け直さないとテスト用の置き場を見てくれない。

        あわせて app/storage.py を「バケット未設定 かつ テスト環境」に固定し、
        ローカル保存の経路に倒す（GCSへ実通信させないため）。

        DBの用意は共通フィクスチャ temp_db に任せている
        （テスト用PostgreSQLのテーブルを空にしてから始める）。
    """
    test_upload_dir = tmp_path / "uploads"
    test_upload_dir.mkdir(parents=True)
    monkeypatch.setattr(upload_paths, "UPLOAD_DIR", test_upload_dir)
    monkeypatch.setattr(storage, "GCS_BUCKET_NAME", "")
    monkeypatch.setattr(storage, "APP_ENV", "test")

    # 管理者チェックを通す（権限そのものを見るテストでは、この上書きを個別に外す）
    app.dependency_overrides[require_ceo] = lambda: {"user_id": "ADMIN"}

    yield test_upload_dir

    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _safe_save_name(suffix: str = ".txt") -> str:
    """アップロード時と同じ生成規則で、安全な保存ファイル名を1つ作る。"""
    return f"{'2' * 20}_{uuid.uuid4()}{suffix}"


def _insert_source(file_name: str, file_type: str, file_path: str) -> int:
    """sources テーブルに1行だけ直接登録して source_id を返す。

    アップロードAPI経由にしない理由:
        このテストで確かめたいのは「壊れた file_path や URLソースを渡されたとき
        ダウンロード側がどう振る舞うか」。正規のアップロードでは作れない行を
        意図的に用意する必要があるため、DBへ直接入れる。
    """
    conn = database.get_connection()
    cursor = conn.execute(
        """
        INSERT INTO sources
            (file_name, file_type, file_path, scope, owner_user_id, uploaded_at, uploaded_by)
        VALUES (%s, %s, %s, 'common', NULL, '2026-01-01T00:00:00+00:00', 'ADMIN')
        RETURNING source_id
        """,
        (file_name, file_type, file_path),
    )
    row = cursor.fetchone()
    conn.commit()
    conn.close()
    assert row is not None
    source_id: int = row["source_id"]
    return source_id


def _put_file(upload_dir: Path, body: str = "これはテスト用の本文です") -> str:
    """テスト用の実ファイルを uploads/ に置き、その保存パス文字列を返す。"""
    save_name = _safe_save_name()
    (upload_dir / save_name).write_text(body, encoding="utf-8")
    return str(upload_dir / save_name)


def test_正常系_実ファイルが元のファイル名でダウンロードできる(
    client: TestClient, upload_dir: Path
) -> None:
    """中身がそのまま返り、ブラウザに提示される名前は元のファイル名になる。"""
    file_path = _put_file(upload_dir, "就業規則の本文です")
    source_id = _insert_source("就業規則.txt", "txt", file_path)

    response = client.get(f"/api/sources/{source_id}/download")

    assert response.status_code == 200, response.text
    assert response.text == "就業規則の本文です"

    # 日本語名はパーセントエンコードされて filename* 形式で載る（RFC5987）
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename" in disposition

    # ディスク上の自動生成名は外に出さない（元のファイル名だけを見せる）
    assert Path(file_path).name not in disposition


def test_社員はダウンロードできない(client: TestClient, upload_dir: Path) -> None:
    """require_ceo が効いていること。社員トークンでは403になる。"""
    file_path = _put_file(upload_dir)
    source_id = _insert_source("給与明細.txt", "txt", file_path)

    # 管理者素通りの上書きを外し、代わりに「社員としてログイン済み」を差し込む
    app.dependency_overrides.pop(require_ceo, None)
    app.dependency_overrides[verify_token] = lambda: {"user_id": "EMP001", "role": "employee"}

    response = client.get(f"/api/sources/{source_id}/download")

    assert response.status_code == 403, response.text


def test_存在しないsource_idは404(client: TestClient, upload_dir: Path) -> None:
    response = client.get("/api/sources/99999/download")

    assert response.status_code == 404, response.text


def test_URLソースはダウンロードできない(client: TestClient, upload_dir: Path) -> None:
    """URLソースの file_path はユーザー入力そのもの。パス処理へ流す前に弾く。"""
    source_id = _insert_source("会社ホームページ", "url", "https://example.com/about")

    response = client.get(f"/api/sources/{source_id}/download")

    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "危険なfile_path",
    [
        "/etc/passwd",  # 絶対パスで uploads/ の外を指す
        "../../secret.txt",  # 相対パスで上の階層へ抜ける
        "uploads/../../secret.txt",
        "uploads/../../../etc/passwd",
        "uploads/evil.txt",  # uploads/ の中だが生成規則に合わない名前
        "uploads/..\\..\\secret.txt",  # Windows形式の区切り文字
    ],
)
def test_想定外のfile_pathは404で読み出せない(
    client: TestClient, upload_dir: Path, tmp_path: Path, 危険なfile_path: str
) -> None:
    """DBの file_path が汚染されていても uploads/ の外を読み出させない。

    実際に読めてしまわないか確かめるため、狙われる位置に本物のファイルを置いておく。
    """
    # uploads/ の外（tmp_path直下）と、uploads/ 内の生成規則外の名前に囮を置く
    (tmp_path / "secret.txt").write_text("これは漏れてはいけない内容です", encoding="utf-8")
    (upload_dir / "evil.txt").write_text("これも漏れてはいけない内容です", encoding="utf-8")

    source_id = _insert_source("見た目は普通.txt", "txt", 危険なfile_path)

    response = client.get(f"/api/sources/{source_id}/download")

    assert response.status_code == 404, response.text
    assert "漏れてはいけない" not in response.text


# ヘッダへの注入を狙った表示名の一覧。
# エンドポイント経由のテストと、ヘッダ組み立て関数単体のテストで同じ入力を使う
危険なfile_name一覧 = [
    'evil"; x=y.txt',  # ヘッダの値を閉じて別の属性を差し込む
    "evil\r\nX-Injected: 1.txt",  # 改行でヘッダを分割する
    "evil\nSet-Cookie: a=b.txt",
    "../../evil.txt",  # 表示名にディレクトリ区切りを混ぜる
    "sub/dir/evil.txt",
]


@pytest.mark.parametrize("危険なfile_name", 危険なfile_name一覧)
def test_危険なfile_nameでもヘッダに注入されない(
    client: TestClient, upload_dir: Path, 危険なfile_name: str
) -> None:
    """file_name は完全なユーザー入力。ヘッダに載せても1行に収まり、別ヘッダは生えない。"""
    file_path = _put_file(upload_dir)
    source_id = _insert_source(危険なfile_name, "txt", file_path)

    response = client.get(f"/api/sources/{source_id}/download")

    assert response.status_code == 200, response.text

    disposition = response.headers["content-disposition"]

    # ヘッダ値の中に改行が残っていない（残っていればヘッダを分割できてしまう）
    assert "\r" not in disposition
    assert "\n" not in disposition

    # 注入を狙ったヘッダが生えていない
    assert "x-injected" not in response.headers
    assert "set-cookie" not in response.headers

    # 表示名にディレクトリ区切りが残っていない
    assert "/" not in disposition
    assert "\\" not in disposition


def test_file_nameが空でもディスク上の安全な名前で返る(
    client: TestClient, upload_dir: Path
) -> None:
    """表示名として使えない値のときは、代替名（ディスク上の安全な名前）を使う。"""
    file_path = _put_file(upload_dir)
    source_id = _insert_source("   ", "txt", file_path)

    response = client.get(f"/api/sources/{source_id}/download")

    assert response.status_code == 200, response.text
    assert Path(file_path).name in response.headers["content-disposition"]


def test_DBに行はあるが実ファイルが無い場合は404(client: TestClient, upload_dir: Path) -> None:
    """保存名の形式は正しいが、ディスク上のファイルが消えているケース。"""
    save_name = _safe_save_name()  # ファイルは作らない
    source_id = _insert_source("消えた資料.txt", "txt", str(upload_dir / save_name))

    response = client.get(f"/api/sources/{source_id}/download")

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Content-Disposition の組み立て関数（build_content_disposition）単体のテスト
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("危険なfile_name", 危険なfile_name一覧)
def test_ヘッダ組み立て関数は危険な表示名を無害化する(危険なfile_name: str) -> None:
    """FileResponse が担っていた性質を、この関数だけで満たしていることを固定する。

    エンドポイント経由のテストと同じ入力を通す。あちらは「結果として注入されない」を、
    こちらは「組み立て関数の出力そのものが安全である」を確かめる。
    関数単体で押さえておけば、将来この関数を別の場所から使っても性質が保たれる。
    """
    disposition = build_content_disposition(_safe_download_name(危険なfile_name, "fallback.txt"))

    assert disposition.startswith("attachment;")

    # 改行が残っていればヘッダを分割できてしまう
    assert "\r" not in disposition
    assert "\n" not in disposition

    # 表示名にディレクトリ区切りが残っていない
    assert "/" not in disposition
    assert "\\" not in disposition

    # HTTPヘッダは latin-1 で送られる。非ASCIIが生で残っていれば送信時に落ちる
    disposition.encode("latin-1")

    # filename="..." 形式のときは、値の途中でクォートを閉じられていない
    # （閉じられると 'x=y' のような別の属性を差し込めてしまう）
    if 'filename="' in disposition:
        値 = disposition.split('filename="', 1)[1]
        assert 値.count('"') == 1, f"クォートを閉じられている: {disposition}"


def test_ヘッダ組み立て関数は安全な名前をそのまま載せる() -> None:
    """エンコードが要らない名前は、読める形のまま提示する。"""
    assert build_content_disposition("report.pdf") == 'attachment; filename="report.pdf"'


def test_ヘッダ組み立て関数は日本語名をRFC5987形式にする() -> None:
    """非ASCIIはパーセントエンコードし、filename* 形式で載せる。"""
    disposition = build_content_disposition("就業規則.txt")

    assert disposition.startswith("attachment; filename*=utf-8''")
    assert "就業規則" not in disposition  # 生の非ASCIIが残っていない
    disposition.encode("latin-1")
