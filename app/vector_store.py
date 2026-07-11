"""Qdrant（ベクトルDB）操作を担うモジュール。

この段階では「① コレクション（棚）を用意する」処理だけを実装している。
ポイント（ベクトル）の保存や検索は後続ステップで追加する。
"""

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

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


def save_chunks(
    chunks: list[str],
    vectors: list[list[float]],
    source_id: str,
) -> list[str]:
    """1つのソースのチャンク群を、まとめて founder コレクションに保存（upsert）する。

    入力:
        chunks    … 分割済みテキストのリスト（例: ["本文1", "本文2", ...]）
        vectors   … 各チャンクをベクトル化した結果のリスト。chunks と同じ順番・同じ件数
        source_id … このチャンク群が属する元ソースのID（どのファイル由来かを示す）

    出力:
        保存した各ポイントのID（文字列）のリスト。後で確認・削除に使える。

    処理:
        1. chunks と vectors の件数が一致するか確認（ズレは検索結果の取り違えの原因になる）
        2. チャンクごとに「id + ベクトル + payload」の1ポイントを組み立てる
           - id     : source_id とチャンク番号から一意に決まるUUID
           - vector : そのチャンクのベクトル
           - payload: 元の文章(text)と source_id。検索後に元テキストを取り出すため
        3. まとめて upsert（無ければ追加、同じidがあれば上書き）する

    なぜ id を source_id + 番号から作るか:
        同じソースを再アップロードしても同じidになり、重複ではなく上書きになる（冪等）。
    """
    # 件数がズレていると、どのチャンクがどのベクトルか対応が崩れるので先に弾く
    if len(chunks) != len(vectors):
        raise ValueError(
            f"chunks と vectors の件数が一致しません: {len(chunks)} != {len(vectors)}"
        )

    points: list[PointStruct] = []
    ids: list[str] = []

    # チャンクを1件ずつ「保存できる形（ポイント）」に変換する
    for index, (text, vector) in enumerate(zip(chunks, vectors)):
        # source_id とチャンク番号を元に、一意で毎回同じになるIDを生成する
        point_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{source_id}:{index}")
        )
        ids.append(point_id)

        points.append(
            PointStruct(
                id=point_id,
                vector=vector,
                # payload = ベクトルに紐づく付帯情報。検索後にここから元テキストを取り出す
                payload={
                    "text": text,
                    "source_id": source_id,
                },
            )
        )

    # 組み立てたポイントをまとめて保存（同じidは上書き＝冪等）
    _client.upsert(collection_name=COLLECTION_NAME, points=points)

    return ids
