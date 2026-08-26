"""vector_store.search() の検索フィルタのテスト。

対象:
    app.vector_store.search()

何を守りたいか（Issue #24 の完了条件）:
    1. 社員データ画面からの質問（target_user_id 指定）では、
       「全社共通ソース」と「その社員の個別ソース」だけが検索対象になること
    2. 他の社員の個別ソース（評価・給与など）は絶対に検索対象に入らないこと
    3. 社員(employee)は target_user_id を渡されても共通ソースしか見られないこと
    4. 通常チャット（target_user_id なし）の挙動が今までと変わっていないこと

Qdrant Cloud には接続しない:
    _client（Qdrantクライアント）と embed_text（Gemini呼び出し）を monkeypatch で
    差し替え、渡された query_filter の中身をテスト側で検証する。
    外部サービスに繋がないので、ネットワークやAPIキーが無くても常に同じ結果になる。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app import vector_store

# テスト用のダミー資料。payload の形は save_chunks が保存するものと同じ
COMMON_SOURCE: dict[str, Any] = {
    "text": "就業規則：有給休暇は年20日",
    "source_id": "10",
    "scope": "common",
    "owner_user_id": None,
}
TARGET_SOURCE: dict[str, Any] = {
    "text": "奥村さんのQ1評価：目標達成率112%",
    "source_id": "20",
    "scope": "individual",
    "owner_user_id": "2",
}
OTHER_STAFF_SOURCE: dict[str, Any] = {
    "text": "別の社員の評価：改善が必要",
    "source_id": "30",
    "scope": "individual",
    "owner_user_id": "3",
}

ALL_SOURCES = [COMMON_SOURCE, TARGET_SOURCE, OTHER_STAFF_SOURCE]


def _conditions(value: Any) -> list[Any]:
    """Filter の must / should を、必ずリストの形で取り出す。

    Qdrantの型定義では must / should に「条件1件」をそのまま入れることもできるため、
    None・単体・リストの3通りがありうる。テスト側で毎回場合分けすると読みにくいので、
    ここでリストに揃えてしまう。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _field(condition: Any) -> tuple[str, Any]:
    """FieldCondition から (payloadのキー, 一致させたい値) を取り出す。

    search が組み立てるのは「キーの値が◯◯と等しい」条件（MatchValue）だけなので、
    それ以外の条件が来た場合はテストを失敗させる（想定外の条件に気づけるようにする）。
    """
    assert isinstance(condition, FieldCondition), f"FieldCondition ではありません: {condition}"
    assert isinstance(condition.match, MatchValue), f"MatchValue ではありません: {condition.match}"
    return condition.key, condition.match.value


def _matches(condition: Any, payload: dict) -> bool:
    """Qdrantのフィルタ判定を、テスト側で最小限だけ再現する。

    入力:
        condition … Filter か FieldCondition（None は「絞り込みなし」）
        payload   … 判定したいポイントの payload

    出力:
        その payload がフィルタの条件を満たすなら True

    再現している規則（search が使う範囲だけ）:
        - None          … 絞り込みなし。すべて通す
        - FieldCondition … payload の key の値が、指定値と一致すれば True
        - Filter.must    … 並べた条件を「すべて」満たす必要がある（AND）
        - Filter.should  … 並べた条件の「どれか1つ」を満たせばよい（OR）

    なぜテスト側で判定を書くか:
        本物のQdrantに繋がずに「他の社員の個別ソースが結果に混ざらない」ことを
        確かめたいため。フィルタの形だけを見るテストだと、
        条件の意味が逆でも気づけない（例: should と must の取り違え）。
        payload を実際に通してみることで、絞り込みの結果まで固定できる。
    """
    if condition is None:
        return True

    if isinstance(condition, FieldCondition):
        key, expected = _field(condition)
        return payload.get(key) == expected

    assert isinstance(condition, Filter), f"想定外の条件です: {condition}"

    must = _conditions(condition.must)
    if must and not all(_matches(c, payload) for c in must):
        return False

    should = _conditions(condition.should)
    if should and not any(_matches(c, payload) for c in should):
        return False

    return True


class FakeQdrantClient:
    """query_points だけを持つ、Qdrantクライアントの偽物。

    役割:
        - 渡された query_filter を last_query_filter に記録する（フィルタの形の検証用）
        - 手持ちの payload をそのフィルタに通し、通ったものだけを検索結果として返す
          （＝「他人のソースが結果に混ざらない」ことの検証用）
    """

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.last_query_filter: Filter | None = None

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        query_filter: Filter | None,
        limit: int,
        with_payload: bool,
    ) -> SimpleNamespace:
        self.last_query_filter = query_filter

        matched = [p for p in self.payloads if _matches(query_filter, p)]
        points = [SimpleNamespace(id=p["source_id"], payload=p) for p in matched[:limit]]
        return SimpleNamespace(points=points)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeQdrantClient:
    """Qdrant接続とGeminiのベクトル化を、テスト用の偽物に差し替える。"""
    client = FakeQdrantClient(ALL_SOURCES)
    monkeypatch.setattr(vector_store, "_client", client)
    # embed_text は Gemini API を呼ぶので、固定のダミーベクトルに差し替える
    monkeypatch.setattr(vector_store, "embed_text", lambda text: [0.0] * 3)
    return client


def test_対象社員を指定すると共通と本人の個別だけが返る(fake_client: FakeQdrantClient) -> None:
    """他の社員の個別ソースが検索結果に混ざらないことを固定する（Issue #24 の核心）。"""
    results = vector_store.search("評価は？", role="ceo", target_user_id="2", top_k=10)

    source_ids = [r["source_id"] for r in results]

    # 共通ソースと、対象社員（user_id=2）の個別ソースは返る
    assert "10" in source_ids
    assert "20" in source_ids
    # 他の社員（user_id=3）の個別ソースは絶対に返らない
    assert "30" not in source_ids


def test_対象社員のフィルタは共通または本人の個別というORになっている(
    fake_client: FakeQdrantClient,
) -> None:
    """組み立てたフィルタの形そのものを固定する（should=OR / must=AND の取り違え防止）。"""
    vector_store.search("評価は？", role="ceo", target_user_id="2")

    query_filter = fake_client.last_query_filter
    assert isinstance(query_filter, Filter)

    should = _conditions(query_filter.should)
    assert len(should) == 2

    # 条件A: scope == "common"
    assert _field(should[0]) == ("scope", "common")

    # 条件B: scope == "individual" かつ owner_user_id == "2"
    individual_condition = should[1]
    assert isinstance(individual_condition, Filter)
    assert {_field(c) for c in _conditions(individual_condition.must)} == {
        ("scope", "individual"),
        ("owner_user_id", "2"),
    }


def test_社員はtarget_user_idを渡されても共通ソースだけ(fake_client: FakeQdrantClient) -> None:
    """社員ロールでは target_user_id を無視し、個別ソースに一切届かない（二重の守り）。"""
    results = vector_store.search("評価は？", role="employee", target_user_id="2", top_k=10)

    assert [r["source_id"] for r in results] == ["10"]


def test_社員の通常検索は共通ソースだけ(fake_client: FakeQdrantClient) -> None:
    """既存の社員チャットの挙動が変わっていないことを固定する。"""
    results = vector_store.search("有給は？", role="employee", top_k=10)

    assert [r["source_id"] for r in results] == ["10"]
    # 絞り込み条件は「scope == common」の must ひとつだけ（従来どおり）
    query_filter = fake_client.last_query_filter
    assert isinstance(query_filter, Filter)
    assert query_filter.should is None
    assert [_field(c) for c in _conditions(query_filter.must)] == [("scope", "common")]


def test_社長の通常検索は絞り込みなし(fake_client: FakeQdrantClient) -> None:
    """既存の社長チャットの挙動（全ソースが対象）が変わっていないことを固定する。"""
    results = vector_store.search("評価は？", role="ceo", top_k=10)

    assert sorted(r["source_id"] for r in results) == ["10", "20", "30"]
    assert fake_client.last_query_filter is None
