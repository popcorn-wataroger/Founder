"""ストリーミング生成のリトライ挙動のテスト（Issue #110）。

対象:
    app.rag.answer_question_stream の内部関数 generate_chunks

何を守りたいか:
    1. Geminiが 503（ServerError）を返しても、本文をまだ送っていなければリトライして
       最終的に回答を返せること
    2. リトライ上限に達したら、従来どおり例外が呼び出し元まで伝わること
    3. 本文を一部でも送った後にエラーが起きた場合はリトライせず、
       そのまま例外が伝わること（回答が二重に流れるのを防ぐため）

外部サービスには繋がない:
    generate_content_stream と search を monkeypatch で差し替える。
    time.sleep も差し替え、テストが実際に待たされないようにする。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai.errors import ClientError, ServerError

from app import rag


def _server_error() -> ServerError:
    """Gemini APIが混雑時に返す 503 UNAVAILABLE を模したエラーを作る。"""
    return ServerError(
        503,
        {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}},
        None,
    )


def _client_error() -> ClientError:
    """Gemini APIが不正なリクエストに対して返す 4xx 系エラーを模したもの。

    一時的な混雑ではなく呼び出し方の誤りなので、リトライしても無駄なだけでなく
    ユーザーへのエラー通知を遅らせてしまう。ServerError と区別してリトライしないことを確認する。
    """
    return ClientError(
        400,
        {"error": {"code": 400, "message": "invalid request", "status": "INVALID_ARGUMENT"}},
        None,
    )


def _patch_search_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """search が1件ヒットしたことにする（generate_chunks まで到達させるため）。"""

    def fake_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"text": "就業規則：有給休暇は年20日", "source_id": "1"}]

    monkeypatch.setattr(rag, "search", fake_search)


def test_本文を送る前の503はリトライして成功する(monkeypatch: pytest.MonkeyPatch) -> None:
    """1回目・2回目は503で失敗し、3回目で成功する場合、最終的に回答が返る。"""
    _patch_search_hit(monkeypatch)
    call_count = 0

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _server_error()
        return iter([SimpleNamespace(text="（AIの回答）")])

    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: None)

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")
    body = "".join(chunks)

    assert body == "（AIの回答）"
    assert call_count == 3


def test_リトライ上限に達したら例外が伝わる(monkeypatch: pytest.MonkeyPatch) -> None:
    """毎回503が続く場合、上限回数を試したうえで最終的に例外を送出する。"""
    _patch_search_hit(monkeypatch)
    call_count = 0

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        nonlocal call_count
        call_count += 1
        raise _server_error()

    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: None)

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")

    with pytest.raises(ServerError):
        "".join(chunks)

    assert call_count == rag.STREAM_RETRY_MAX_ATTEMPTS


def test_本文送信後の503はリトライしない(monkeypatch: pytest.MonkeyPatch) -> None:
    """途中まで断片を送った後にエラーが起きた場合、やり直すと二重に流れてしまうため
    リトライせずそのまま例外を伝える。
    """
    _patch_search_hit(monkeypatch)
    call_count = 0

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        nonlocal call_count
        call_count += 1

        def _gen() -> Any:
            yield SimpleNamespace(text="途中まで")
            raise _server_error()

        return _gen()

    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: None)

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")

    collected = []
    with pytest.raises(ServerError):
        for chunk in chunks:
            collected.append(chunk)

    assert collected == ["途中まで"]
    # リトライしていない＝1回しか呼ばれていない
    assert call_count == 1


def test_4xx系エラーはリトライしない(monkeypatch: pytest.MonkeyPatch) -> None:
    """ClientError（不正なリクエスト等）は一時的な混雑ではないため、リトライせず即座に伝える。

    ServerError と違って呼び出し方自体が誤っている可能性が高く、
    待ってからやり直しても同じ結果になるだけでなく、ユーザーへのエラー通知を
    無駄に遅らせてしまう。
    """
    _patch_search_hit(monkeypatch)
    call_count = 0

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        nonlocal call_count
        call_count += 1
        raise _client_error()

    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: None)

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")

    with pytest.raises(ClientError):
        "".join(chunks)

    # リトライしていない＝1回しか呼ばれていない
    assert call_count == 1


def test_ServerError以外の例外はリトライしない(monkeypatch: pytest.MonkeyPatch) -> None:
    """想定外のバグ（TypeError等）まで503と同じ扱いでリトライしてしまうと、
    バグを隠してしまう。ServerError 以外はそのまま伝える。
    """
    _patch_search_hit(monkeypatch)
    call_count = 0

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        nonlocal call_count
        call_count += 1
        raise ValueError("想定外のバグ")

    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: None)

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")

    with pytest.raises(ValueError):
        "".join(chunks)

    assert call_count == 1


def test_空のchunkだけを受け取った後の503は本文未送信としてリトライする(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chunk.text が空文字/None の断片（安全性判定のみ等）は、実際には
    ユーザーへ何も送っていない。その後に503が起きても「本文未送信」として
    リトライ対象に含まれることを確認する。
    """
    _patch_search_hit(monkeypatch)
    call_count = 0

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        nonlocal call_count
        call_count += 1

        if call_count == 1:

            def _gen() -> Any:
                # 中身の無い断片（例: 安全性フィルタの判定情報だけ）
                yield SimpleNamespace(text=None)
                yield SimpleNamespace(text="")
                raise _server_error()

            return _gen()
        return iter([SimpleNamespace(text="（AIの回答）")])

    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: None)

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")
    body = "".join(chunks)

    assert body == "（AIの回答）"
    assert call_count == 2


def test_バックオフの待機秒数が試行ごとに倍になる(monkeypatch: pytest.MonkeyPatch) -> None:
    """待機秒数が 2秒 → 4秒 と、試行のたびに倍になっていることを確認する。

    高負荷が原因のエラーに対して間隔を空けずに連打すると、
    Gemini側の混雑をかえって悪化させかねないため、間隔を空ける設計にしている。
    """
    _patch_search_hit(monkeypatch)
    call_count = 0
    sleep_calls: list[float] = []

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _server_error()
        return iter([SimpleNamespace(text="（AIの回答）")])

    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    monkeypatch.setattr(rag.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")
    "".join(chunks)

    assert sleep_calls == [2, 4]
