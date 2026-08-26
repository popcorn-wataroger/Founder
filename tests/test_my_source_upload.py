"""社員が「自分の個別ソース」を扱えるようになったことのテスト。

対象:
    app/vector_store.py の search()      … 検索範囲に自分の個別ソースが入るか
    POST /api/sources/my-upload          … 自分の個別ソースとして登録できるか

何を守りたいか:
    1. 社員は「共通ソース ＋ 自分の個別ソース」だけを検索できること。
       他人の個別ソース（評価・給与）には絶対に届かないこと
    2. 社員の経路では target_user_id が引き続き完全に無視されること。
       ここが破れると、他人を指す値を渡すだけで他人の資料が読めてしまう
    3. アップロードした資料の持ち主が、必ず送信者本人になること。
       リクエストに owner_user_id や scope を付けても、その値は使われないこと

なぜ Qdrant への通信をモックするか:
    確かめたいのは「どんな絞り込み条件（フィルタ）を組み立てたか」であって、
    Qdrant が実際に何を返すかではない。query_points を差し替えて、
    渡された query_filter そのものを検証する。
    実通信しないので、Qdrant が動いていない環境でも実行できる。
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from qdrant_client.models import FieldCondition, Filter

from app import database, storage, upload_paths, vector_store
from app.main import app
from app.routers import sources_router
from app.routers.auth_router import ROLE_CEO, ROLE_EMPLOYEE, create_access_token

# users.csv 上の EMP001（社員）と EMP002（他人）の user_id
EMPLOYEE_USER_ID = "2"
OTHER_USER_ID = "3"


def _headers(user_id: str, role: str) -> dict[str, str]:
    """指定したロールでログイン済みとみなすための Authorization ヘッダを作る。

    入力:
        user_id … トークンに載せる user_id
        role    … トークンに載せるロール名

    出力:
        {"Authorization": "Bearer <JWT>"} の形の辞書

    なぜ dependency_overrides ではなく本物のトークンを使うか:
        my-upload は「トークンの user_id を持ち主にする」エンドポイント。
        差し替えると、その受け渡しごと飛ばしてしまう。
    """
    token = create_access_token(user_id=user_id, role=role)
    return {"Authorization": f"Bearer {token}"}


# ===== 検索範囲のテスト（app/vector_store.py の search()）=====


class _FakeResponse:
    """query_points の戻り値の代わり。検索結果は空でよい。

    このテストが見たいのは「渡したフィルタ」であって検索結果ではないため、
    points は空リストにしてある（空なら search は空リストを返して終わる）。
    """

    def __init__(self) -> None:
        self.points: list[object] = []


def _capture_query_filter(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """query_points に渡された引数を記録する偽物へ差し替える。

    入力:
        monkeypatch … pytest の差し替え機構

    出力:
        呼び出し時の引数が入る辞書（呼ばれた後に "query_filter" を見る）

    embed_text も差し替えるのは、本物だと Gemini API へ実通信してしまうため。
    ベクトルの中身は検証対象ではないので、固定の短い数列で足りる。
    """
    captured: dict[str, Any] = {}

    def fake_embed_text(text: str) -> list[float]:
        return [0.0, 0.0, 0.0]

    def fake_query_points(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse()

    monkeypatch.setattr(vector_store, "embed_text", fake_embed_text)
    monkeypatch.setattr(vector_store._client, "query_points", fake_query_points)

    return captured


def _collect_conditions(query_filter: Filter | None) -> list[tuple[str, str]]:
    """フィルタに含まれる条件を (項目名, 値) の組で全部取り出す。

    入力:
        query_filter … search が組み立てた Filter（絞り込みなしなら None）

    出力:
        [("scope", "common"), ("owner_user_id", "2"), ...] の形のリスト

    入れ子（should の中の Filter）も再帰でたどるのは、
    「共通 または（個別 かつ 本人）」という条件が入れ子で表現されているため。
    平らにして見ることで、「どのIDがフィルタに現れたか」を漏れなく確認できる。
    """
    if query_filter is None:
        return []

    pairs: list[tuple[str, str]] = []
    for group in (query_filter.must, query_filter.should, query_filter.must_not):
        for condition in group or []:
            if isinstance(condition, Filter):
                pairs.extend(_collect_conditions(condition))
            elif isinstance(condition, FieldCondition) and condition.match is not None:
                pairs.append((condition.key, str(condition.match.value)))
    return pairs


def test_社員は共通と自分の個別ソースを検索できる(monkeypatch: pytest.MonkeyPatch) -> None:
    """self_user_id を渡すと、共通ソースに「自分の個別ソース」が加わる。"""
    captured = _capture_query_filter(monkeypatch)

    vector_store.search("就業規則は？", role=ROLE_EMPLOYEE, self_user_id=EMPLOYEE_USER_ID)

    conditions = _collect_conditions(captured["query_filter"])

    # 共通ソース
    assert ("scope", "common") in conditions
    # 個別ソース かつ 持ち主が自分
    assert ("scope", "individual") in conditions
    assert ("owner_user_id", EMPLOYEE_USER_ID) in conditions


def test_社員の経路ではtarget_user_idが無視される(monkeypatch: pytest.MonkeyPatch) -> None:
    """他人を指す target_user_id を渡しても、フィルタには一切現れない。

    なぜ重要か:
        ここが破れると、社員が他人の user_id を渡すだけで
        その人の個別ソース（評価・給与）を読めてしまう。
        search は社員の経路で target_user_id を見ない作りになっており、
        その設計がこのテストで固定される。
    """
    captured = _capture_query_filter(monkeypatch)

    vector_store.search(
        "他人の評価は？",
        role=ROLE_EMPLOYEE,
        target_user_id=OTHER_USER_ID,
        self_user_id=EMPLOYEE_USER_ID,
    )

    conditions = _collect_conditions(captured["query_filter"])
    値の一覧 = [value for _, value in conditions]

    # 他人の user_id はどの条件にも現れない
    assert OTHER_USER_ID not in 値の一覧
    # 自分の user_id は現れる（自分の個別ソースは対象）
    assert EMPLOYEE_USER_ID in 値の一覧


def test_self_user_idが無ければ共通ソースのみ(monkeypatch: pytest.MonkeyPatch) -> None:
    """従来どおりの挙動（共通ソース固定）が壊れていないことを確かめる。"""
    captured = _capture_query_filter(monkeypatch)

    vector_store.search("就業規則は？", role=ROLE_EMPLOYEE)

    conditions = _collect_conditions(captured["query_filter"])

    # 条件は scope='common' の1つだけ。個別ソースには一切触れない
    assert conditions == [("scope", "common")]


def test_社長がtarget_user_idを指定した場合は従来どおり(monkeypatch: pytest.MonkeyPatch) -> None:
    """社員データ画面からの問い合わせ（共通 ＋ 対象社員の個別）が壊れていないこと。"""
    captured = _capture_query_filter(monkeypatch)

    vector_store.search("この社員について", role=ROLE_CEO, target_user_id=OTHER_USER_ID)

    conditions = _collect_conditions(captured["query_filter"])

    assert ("scope", "common") in conditions
    assert ("scope", "individual") in conditions
    assert ("owner_user_id", OTHER_USER_ID) in conditions


# ===== アップロードのテスト（POST /api/sources/my-upload）=====


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, temp_db: None) -> Iterator[Path]:
    """本番の uploads/ を汚さないよう、使い捨ての置き場に差し替える。

    出力:
        テスト用の uploads ディレクトリのパス

    tests/test_sources_upload.py の同名フィクスチャと同じ考え方。
    保存先を tmp_path 配下へ向け、ベクトル化（Gemini・Qdrantへの実通信）を差し替える。
    認証は差し替えない。このエンドポイントは「トークンの user_id を持ち主にする」ため、
    そこを本物のまま通す必要がある。
    """
    test_upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(upload_paths, "UPLOAD_DIR", test_upload_dir)
    monkeypatch.setattr(storage, "GCS_BUCKET_NAME", "")
    monkeypatch.setattr(storage, "APP_ENV", "test")

    def fake_vectorize_and_save(**kwargs: object) -> int:
        return 1

    monkeypatch.setattr(sources_router, "_vectorize_and_save", fake_vectorize_and_save)

    yield test_upload_dir


def _upload_my_source(
    file_name: str = "私の資格証明.txt",
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
) -> httpx.Response:
    """自分の個別ソースとしてファイルを登録するAPIを叩く（テスト用の短縮形）。"""
    return TestClient(app).post(
        "/api/sources/my-upload",
        files={"file": (file_name, "これはテスト用の本文です".encode(), "text/plain")},
        data=data or {},
        headers=headers if headers is not None else _headers(EMPLOYEE_USER_ID, ROLE_EMPLOYEE),
    )


def _fetch_source(source_id: int) -> dict[str, Any]:
    """登録されたソースの行をDBから取り出す。"""
    conn = database.get_connection()
    row = conn.execute(
        "SELECT scope, owner_user_id, uploaded_by, file_name FROM sources WHERE source_id = %s",
        (source_id,),
    ).fetchone()
    conn.close()
    assert row is not None, f"source_id={source_id} の行がDBに存在しない"
    return dict(row)


def test_自分の個別ソースとして登録される(upload_dir: Path) -> None:
    """社員がアップロードすると scope='individual'、持ち主は本人になる。"""
    response = _upload_my_source()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True

    row = _fetch_source(body["source_id"])

    assert row["scope"] == "individual"
    assert row["owner_user_id"] == EMPLOYEE_USER_ID
    assert row["uploaded_by"] == EMPLOYEE_USER_ID
    # 表示名は元のファイル名のまま
    assert row["file_name"] == "私の資格証明.txt"


def test_送られたowner_user_idとscopeは無視される(upload_dir: Path) -> None:
    """他人の user_id や scope='common' をフォームで送っても、その値は使われない。

    なぜ重要か:
        このエンドポイントは scope と owner_user_id を引数で受け取らない作りにしてある。
        「送られた値を検証で弾く」のではなく「値を送れる経路そのものを作らない」設計で、
        余計なフォーム項目を付けても素通りすることを、ここで固定する。
    """
    response = _upload_my_source(
        data={"owner_user_id": OTHER_USER_ID, "scope": "common"},
    )

    assert response.status_code == 200, response.text
    row = _fetch_source(response.json()["source_id"])

    # 送った値ではなく、トークンの user_id が持ち主になる
    assert row["owner_user_id"] == EMPLOYEE_USER_ID
    assert row["owner_user_id"] != OTHER_USER_ID
    # 送った scope='common' も使われない
    assert row["scope"] == "individual"


def test_未ログインは401(upload_dir: Path) -> None:
    """トークンが無ければ、誰の資料か決められないので受け付けない。"""
    response = _upload_my_source(headers={})

    assert response.status_code == 401, response.text


def test_対応外の拡張子は400(upload_dir: Path) -> None:
    """対応形式（pdf / docx / pptx / txt）以外は登録できない。"""
    response = _upload_my_source(file_name="悪意のあるファイル.exe")

    assert response.status_code == 400, response.text
