from app.harness.agents.evidence_matching import resolve_verbatim_evidence_quote


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
