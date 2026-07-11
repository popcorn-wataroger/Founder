"""Qdrant（ベクトルDB）操作を担うモジュール。

この段階では「① コレクション（棚）を用意する」処理だけを実装している。
ポイント（ベクトル）の保存や検索は後続ステップで追加する。
"""

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from app.config import QDRANT_API_KEY, QDRANT_URL

# コレクション名。Qdrant上で「棚」を識別する名前
COLLECTION_NAME = "founder"

# ベクトルの次元数。gemini-embedding-001 が返す 3072 次元に合わせる
VECTOR_SIZE = 3072

# ベクトル同士の「近さ」の測り方。意味の近さを測るRAGでは cosine（コサイン類似度）が定番
DISTANCE = Distance.COSINE

# Qdrant Cloud への接続クライアント。接続情報は config 経由で読み込む（コードに直書きしない）
_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


def ensure_collection() -> None:
    """コレクション（棚）が無ければ作る。あれば何もしない。

    入力:
        なし

    出力:
        なし（Qdrant側にコレクションが用意された状態になる）

    処理:
        1. 既存コレクション一覧を取得し、"founder" が既にあるか調べる
        2. 無ければ、3072次元・cosine距離のコレクションを新規作成する
        3. あれば作らない（何度呼んでも安全に動く＝冪等）

    なぜ冪等にするか:
        アプリ起動のたびに呼んでも、既存コレクションを壊さない・重複作成しないため。
    """
    # 既存コレクション名の一覧を集める
    existing = {c.name for c in _client.get_collections().collections}

    # 既に "founder" があれば、何もせず終了
    if COLLECTION_NAME in existing:
        return

    # 無ければ、次元数と距離を指定してコレクションを新規作成する
    _client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=DISTANCE),
    )
