from app.harness.agents.evidence_matching import (
    materialize_semantic_evidence_quote,
    resolve_semantic_choice,
    resolve_semantic_evidence_quote,
    resolve_verbatim_evidence_quote,
)


def test_resolve_verbatim_evidence_quote_normalizes_typography_and_whitespace() -> None:
    draft = "第一行。\n\n“现有痕迹回答不了。”"

    assert resolve_verbatim_evidence_quote(
        draft,
        '第一行.\n “现有痕迹回答不了.”',
    ) == "第一行。\n\n“现有痕迹回答不了。”"


def test_resolve_verbatim_evidence_quote_normalizes_unique_quote_boundary() -> None:
    draft = (
        "许青调出排班。“扫描间三点四十结束。"
        "四点整，我在一楼北侧核验台等你。登记编号。”"
    )

    assert resolve_verbatim_evidence_quote(
        draft,
        "“四点整，我在一楼北侧核验台等你。",
    ) == "四点整，我在一楼北侧核验台等你。"


def test_quote_boundary_normalization_still_requires_a_unique_match() -> None:
    assert (
        resolve_verbatim_evidence_quote(
            "前文。四点整等你。后文。四点整等你。",
            "“四点整等你。",
        )
        is None
    )


def test_resolve_verbatim_evidence_quote_rejects_semantic_paraphrase() -> None:
    assert (
        resolve_verbatim_evidence_quote(
            "现有痕迹回答不了。",
            "目前证据无法回答。",
        )
        is None
    )


def test_resolve_verbatim_evidence_quote_requires_unique_match() -> None:
    assert resolve_verbatim_evidence_quote("重复。重复。", "重复.") is None


def test_semantic_choice_accepts_a_low_score_when_one_option_is_dominant() -> None:
    temporary_title = "书名目前为暂定《退潮前的十一分钟》，尚未定稿。"
    choices = {
        temporary_title: [temporary_title],
        "结局必须保留真实代价。": ["结局必须保留真实代价。"],
        "故事发生在封闭潮汐站。": ["故事发生在封闭潮汐站。"],
    }

    assert resolve_semantic_choice(
        "书名仍停留在暂定状态，尚未作为最终书名提交。",
        choices,
    ) == temporary_title


def test_semantic_choice_still_rejects_an_ambiguous_hint() -> None:
    choices = {
        "采用第一人称叙事。": ["采用第一人称叙事。"],
        "采用第三人称叙事。": ["采用第三人称叙事。"],
    }

    assert resolve_semantic_choice("采用人称叙事。", choices) is None


def test_evidence_materialization_owns_exact_binding_after_semantic_review() -> None:
    draft = "钟响了一次。钟响了两次。"
    hint = "钟声宣告程序节点。"

    assert resolve_semantic_evidence_quote(draft, [hint]) is None
    assert materialize_semantic_evidence_quote(draft, [hint]) == "钟响了一次。"
