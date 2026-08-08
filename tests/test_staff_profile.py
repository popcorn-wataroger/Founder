"""社員の基本情報をAIに渡す仕組みのテスト（Issue #72）。

対象:
    app.users.format_user_profile … 社員1人分の基本情報をプロンプト用の文字列にする
    app.rag._build_prompt         … 基本情報と社内文書をプロンプトに組み立てる

何を守りたいか:
    1. 平文パスワードなど、渡してはいけない項目がプロンプトに混ざらないこと
    2. 値が空の項目は行ごと省かれること（空欄をAIに解釈させない）
    3. 基本情報と社内文書が別のセクションとして渡ること
    4. 片方しか無いときに、空のセクションを渡さないこと

外部サービスには繋がない:
    どちらの関数も文字列を組み立てるだけで、GeminiにもQdrantにも接続しない。
    そのためAPIキーやDBが無くても実行できる（temp_db も不要）。

APIを通した状態での確認は tests/test_staff_inquiry.py 側にある。
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app import rag
from app.rag import _build_prompt
from app.users import format_user_profile, get_user_by_id

# users.csv 上の想定: user_id=2 は EMP001（奥村仁哉、家族構成あり）
STAFF_WITH_FAMILY = "2"
# users.csv 上の想定: user_id=3 は EMP002（田中美咲、家族構成が空欄）
STAFF_WITHOUT_FAMILY = "3"


def test_基本情報がラベル付きの行として並ぶ() -> None:
    """AIが項目名と値の対応を読み取れる形になっていることを確かめる。"""
    user = get_user_by_id(STAFF_WITH_FAMILY)
    assert user is not None

    profile = format_user_profile(user)

    assert "氏名: 奥村仁哉" in profile
    assert "社員コード: EMP001" in profile
    assert "部署: 営業部" in profile
    assert "家族構成: 配偶者・子1" in profile
    assert "入社日: 2020-04-01" in profile


def test_パスワードとロールは含まれない() -> None:
    """ここが最重要。users.csv の password は平文なので、渡すと回答文に出かねない。

    role も業務上の質問に無関係な内部の区分で、渡す理由がない。
    ホワイトリスト（PROFILE_FIELD_LABELS）に足さない限り漏れない作りになっている。
    """
    user = get_user_by_id(STAFF_WITH_FAMILY)
    assert user is not None

    profile = format_user_profile(user)

    assert "password" not in profile
    assert "employee" not in profile
    # ラベル自体も出てこない（「パスワード:」のような行が無い）
    assert "パスワード" not in profile


def test_値が空の項目は行ごと省かれる() -> None:
    """空欄を「家族構成: 」の形で渡すと、AIが空欄を値と解釈しかねない。

    行ごと省けば、AIから見て「その情報は与えられていない」状態になり、
    プロンプトの指示どおり「記載がありません」と答えられる。
    """
    user = get_user_by_id(STAFF_WITHOUT_FAMILY)
    assert user is not None

    profile = format_user_profile(user)

    assert "氏名: 田中美咲" in profile
    assert "家族構成" not in profile


def test_プロンプトに基本情報と社内文書が別セクションで入る() -> None:
    """出どころの違う情報を分けて渡し、AIが根拠を書き分けられるようにする。"""
    prompt = _build_prompt(
        "家族構成は？",
        ["就業規則：有給休暇は年20日"],
        profile="氏名: 奥村仁哉\n家族構成: 配偶者・子1",
    )

    assert "=== 対象社員の基本情報 ===" in prompt
    assert "家族構成: 配偶者・子1" in prompt
    assert "=== 社内文書 ===" in prompt
    assert "就業規則：有給休暇は年20日" in prompt
    assert "=== 質問 ===\n家族構成は？" in prompt

    # 基本情報が社内文書より先に来る（質問の主語になることが多いため）
    assert prompt.index("=== 対象社員の基本情報 ===") < prompt.index("=== 社内文書 ===")


def test_基本情報が無いときはそのセクションを出さない() -> None:
    """通常のチャット（profile なし）では、今までと同じプロンプトになる。"""
    prompt = _build_prompt("有給は何日？", ["就業規則：有給休暇は年20日"])

    assert "=== 対象社員の基本情報 ===" not in prompt
    assert "=== 社内文書 ===" in prompt


def test_社内文書が無いときはそのセクションを出さない() -> None:
    """関連文書が0件でも、基本情報だけで回答を組み立てられるようにする。

    空の「=== 社内文書 ===」を渡すと、見出しだけあって中身が無い状態になり、
    AIが何を渡されたのか判断しにくくなる。
    """
    prompt = _build_prompt("家族構成は？", [], profile="氏名: 奥村仁哉\n家族構成: 配偶者・子1")

    assert "=== 社内文書 ===" not in prompt
    assert "=== 対象社員の基本情報 ===" in prompt
    assert "家族構成: 配偶者・子1" in prompt


def _patch_rag(monkeypatch: pytest.MonkeyPatch, hits: list[dict[str, Any]]) -> list[str]:
    """検索とGeminiを偽物に差し替え、生成に渡されたプロンプトを記録する。

    入力:
        hits … search が返したことにするチャンク（空リストなら「関連文書0件」）
    出力:
        生成に渡されたプロンプトが入るリスト。生成が呼ばれなければ空のまま
    """
    prompts: list[str] = []

    def fake_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return hits

    def fake_generate_content_stream(model: str, contents: str) -> Any:
        prompts.append(contents)
        return iter([SimpleNamespace(text="（AIの回答）")])

    monkeypatch.setattr(rag, "search", fake_search)
    monkeypatch.setattr(rag._client.models, "generate_content_stream", fake_generate_content_stream)
    return prompts


def test_関連文書が0件でも基本情報があれば生成する(monkeypatch: pytest.MonkeyPatch) -> None:
    """答えが社員マスタ側にしか無い質問（家族構成など）に答えられるようにする。

    ここで打ち切ると、画面に出ている情報なのにAIは「資料がありません」と答えることになり、
    基本情報を渡した意味が無くなる。
    """
    prompts = _patch_rag(monkeypatch, hits=[])

    referenced_sources, chunks = rag.answer_question_stream(
        "家族構成は？",
        role="admin",
        target_user_id="2",
        profile="氏名: 奥村仁哉\n家族構成: 配偶者・子1",
    )
    body = "".join(chunks)

    # 生成が実際に走り、定型文ではなくAIの回答が返る
    assert body == "（AIの回答）"
    assert len(prompts) == 1
    assert "家族構成: 配偶者・子1" in prompts[0]
    # 基本情報は検索結果ではないので、参照ソースは空のまま
    assert referenced_sources == []


def test_文書も基本情報も無ければ生成しない(monkeypatch: pytest.MonkeyPatch) -> None:
    """根拠がゼロの状態で生成させると、AIが自分の知識で答えを作ってしまう。"""
    prompts = _patch_rag(monkeypatch, hits=[])

    referenced_sources, chunks = rag.answer_question_stream("有給は何日？", role="employee")
    body = "".join(chunks)

    assert body == rag.NO_CONTEXT_MESSAGE
    assert prompts == []
    assert referenced_sources == []
