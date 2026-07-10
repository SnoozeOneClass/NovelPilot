import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from app.llm.gateway import ChatMessage, ChatRequest, ChatResult, call_llm
from app.llm.redaction import redact_profile_secrets
from app.schemas.profiles import LlmProfile
from app.schemas.setup import (
    BookDirectionConstraints,
    BookDirectionReview,
    BookDirectionReviewIssue,
    ConfirmedDecisionCoverage,
    SetupMessage,
    SetupReadinessSignal,
    SetupStateDocument,
    SetupSuggestion,
    SupersededDecision,
)


class BookLoop:
    """Owns book-level discovery, candidate direction, and approval boundaries."""


RECENT_MESSAGE_LIMIT = 10
RECENT_MESSAGE_CHARACTER_BUDGET = 16_000
DISCUSSION_CONTEXT_CHARACTER_BUDGET = 96_000
SYNTHESIS_CONTEXT_CHARACTER_BUDGET = 72_000
REVIEW_CONTEXT_CHARACTER_BUDGET = 96_000
MAX_DIRECTION_DRAFT_CHARACTERS = 24_000
MAX_DISCUSSION_SUMMARY_CHARACTERS = 8_000
MAX_DECISION_STATE_CHARACTERS = 20_000
SUPERSEDED_HISTORY_CHARACTER_BUDGET = 8_000


@dataclass(frozen=True)
class DiscussionContextAssembly:
    prompt: str
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class BookDiscussionTurnResult:
    reply: str
    direction_draft: str
    discussion_summary: str
    confirmed_decisions: list[str]
    superseded_decisions: list[SupersededDecision]
    unresolved_questions: list[str]
    assumptions: list[str]
    contradictions: list[str]
    suggestions: list[SetupSuggestion]
    readiness: SetupReadinessSignal
    model_snapshot: str
    provider_snapshot: str
    usage: dict[str, Any]


@dataclass(frozen=True)
class BookDirectionSynthesis:
    direction_markdown: str
    constraints: BookDirectionConstraints
    confirmed_decision_coverage: list[ConfirmedDecisionCoverage]
    rolling_plan_markdown: str
    model_snapshot: str
    provider_snapshot: str
    usage: dict[str, Any]


def assemble_discussion_context(
    state: SetupStateDocument,
    user_message: str,
) -> DiscussionContextAssembly:
    superseded_payload = _recent_superseded_payload(state)
    fixed_blocks = [
        ("discussion_summary", state.discussion_summary or "尚无摘要。"),
        ("current_direction_draft", state.direction_draft or "尚未形成草稿。"),
        ("confirmed_decisions", _json_text(state.confirmed_decisions)),
        ("unresolved_questions", _json_text(state.unresolved_questions)),
        ("assumptions", _json_text(state.assumptions)),
        ("contradictions", _json_text(state.contradictions)),
        ("recent_superseded_decisions", _json_text(superseded_payload)),
        ("current_user_message", user_message),
    ]
    fixed_character_count = sum(len(content) for _, content in fixed_blocks)
    available_recent_characters = min(
        RECENT_MESSAGE_CHARACTER_BUDGET,
        max(0, DISCUSSION_CONTEXT_CHARACTER_BUDGET - fixed_character_count - 2_000),
    )
    recent_reversed: list[SetupMessage] = []
    recent_chars = 0
    for message in reversed(state.messages):
        if len(recent_reversed) >= RECENT_MESSAGE_LIMIT:
            break
        message_chars = len(message.content)
        if recent_chars + message_chars > available_recent_characters:
            break
        recent_reversed.append(message)
        recent_chars += message_chars

    recent = list(reversed(recent_reversed))
    while True:
        recent_payload = [
            {"id": message.id, "role": message.role, "content": message.content}
            for message in recent
        ]
        prompt = "\n\n".join(
            [
                "下面是 Harness 为本轮全书共创装配的上下文。",
                f"讨论摘要：\n{state.discussion_summary or '尚无摘要。'}",
                f"当前完整 Book Direction 草稿：\n{state.direction_draft or '尚未形成草稿。'}",
                "已确认决定：\n" + _json_text(state.confirmed_decisions),
                "待澄清问题：\n" + _json_text(state.unresolved_questions),
                "当前假设：\n" + _json_text(state.assumptions),
                "已发现矛盾：\n" + _json_text(state.contradictions),
                "近期已取代决定：\n" + _json_text(superseded_payload),
                "最近原始对话：\n" + _json_text(recent_payload),
                f"用户本轮输入：\n{user_message}",
            ]
        )
        if len(prompt) <= DISCUSSION_CONTEXT_CHARACTER_BUDGET:
            break
        if not recent:
            raise ValueError("Book discussion state exceeds the total context budget.")
        recent.pop(0)
    recent_chars = sum(len(message.content) for message in recent)
    recent_ids = {message.id for message in recent}
    older = [message for message in state.messages if message.id not in recent_ids]
    version_paths = _discussion_version_paths(state)

    snapshot = {
        "schema_version": 1,
        "loop_layer": "book",
        "atomic_action": "continue_book_discussion",
        "state_revision": state.revision,
        "sources": [
            {
                "id": "book-direction-draft",
                "path": "book/direction_draft.md",
                "resolved_version_path": version_paths["direction"],
                "version": state.revision,
                "usage": "direct",
                "included_fields": ["full_text"],
            },
            {
                "id": "book-discussion-state",
                "path": "book/setup.json",
                "resolved_version_path": version_paths["state"],
                "version": state.revision,
                "usage": "direct",
                "included_fields": [
                    "discussion_summary",
                    "confirmed_decisions",
                    "superseded_decisions",
                    "unresolved_questions",
                    "assumptions",
                    "contradictions",
                ],
            },
            {
                "id": "recent-book-discussion",
                "path": "book/discussion/transcript.jsonl",
                "resolved_version_path": version_paths["transcript"],
                "version": state.turn_count,
                "usage": "direct",
                "included_message_ids": [message.id for message in recent],
            },
            {
                "id": "older-book-discussion",
                "path": "book/discussion/transcript.jsonl",
                "resolved_version_path": version_paths["transcript"],
                "version": state.turn_count,
                "usage": "summary",
                "included_message_ids": [message.id for message in older],
            },
            {
                "id": "current-user-message",
                "path": None,
                "usage": "direct",
                "character_count": len(user_message),
            },
        ],
        "injected": [
            _injected_content_record(
                content_id="current_direction_draft",
                content=state.direction_draft or "尚未形成草稿。",
                source_path=version_paths["direction"],
                version=state.revision,
            ),
            _injected_content_record(
                content_id="discussion_summary",
                content=state.discussion_summary or "尚无摘要。",
                source_path=version_paths["state"],
                version=state.revision,
            ),
            *[
                _injected_content_record(
                    content_id=content_id,
                    content=content,
                    source_path=version_paths["state"],
                    version=state.revision,
                )
                for content_id, content in fixed_blocks[2:7]
            ],
            _injected_content_record(
                content_id="recent_raw_messages",
                content=_json_text(recent_payload),
                source_path=version_paths["transcript"],
                version=state.turn_count,
            ),
            _injected_content_record(
                content_id="current_user_message",
                content=user_message,
                source_path=None,
                version=state.turn_count + 1,
            ),
        ],
        "summarized": [message.id for message in older],
        "excluded": [
            {
                "id": message.id,
                "reason": "Older raw turn represented by the maintained discussion summary.",
            }
            for message in older
        ],
        "budget": {
            "recent_message_limit": RECENT_MESSAGE_LIMIT,
            "recent_character_budget": RECENT_MESSAGE_CHARACTER_BUDGET,
            "recent_character_count": recent_chars,
            "total_character_budget": DISCUSSION_CONTEXT_CHARACTER_BUDGET,
            "total_character_count": len(prompt),
        },
        "assembly_rationale": (
            "Keep the complete candidate direction and compact discussion memory visible, add "
            "only the most recent raw turns, and exclude older raw dialogue already represented "
            "by the maintained summary. The complete transcript remains on disk for audit."
        ),
    }
    return DiscussionContextAssembly(prompt=prompt, snapshot=snapshot)


def continue_book_discussion(
    profile: LlmProfile,
    state: SetupStateDocument,
    user_message: str,
    assembly: DiscussionContextAssembly,
) -> BookDiscussionTurnResult:
    result = call_llm(
        profile,
        ChatRequest(
            profile_id=profile.id,
            messages=[
                ChatMessage(role="system", content=_discussion_system_prompt()),
                ChatMessage(role="user", content=assembly.prompt),
            ],
            response_format={"type": "json_object"},
            temperature=0.55,
            metadata={
                "loop_layer": "book",
                "atomic_action": "continue_book_discussion",
                "turn": state.turn_count + 1,
                "max_tokens": 5000,
            },
        ),
    )
    payload = _parse_json_object(redact_profile_secrets(result.content, profile))
    return _discussion_result_from_payload(
        payload,
        result,
        state.turn_count + 1,
        user_message,
    )


def synthesize_book_direction(
    profile: LlmProfile,
    state: SetupStateDocument,
) -> BookDirectionSynthesis:
    result = call_llm(
        profile,
        ChatRequest(
            profile_id=profile.id,
            messages=[
                ChatMessage(role="system", content=_synthesis_system_prompt()),
                ChatMessage(role="user", content=_render_synthesis_context(state)),
            ],
            response_format={"type": "json_object"},
            temperature=0.35,
            metadata={
                "loop_layer": "book",
                "atomic_action": "synthesize_book_direction",
                "candidate_revision": _next_candidate_revision(state),
                "max_tokens": 7000,
            },
        ),
    )
    payload = _parse_json_object(redact_profile_secrets(result.content, profile))
    return BookDirectionSynthesis(
        direction_markdown=_required_string(
            payload.get("direction_markdown"),
            "direction_markdown",
            max_characters=MAX_DIRECTION_DRAFT_CHARACTERS,
        ),
        constraints=_constraints_from_payload(payload.get("constraints")),
        confirmed_decision_coverage=_coverage_from_payload(
            payload.get("confirmed_decision_coverage")
        ),
        rolling_plan_markdown=_required_string(
            payload.get("rolling_plan_markdown"),
            "rolling_plan_markdown",
            max_characters=12_000,
        ),
        model_snapshot=result.model_snapshot,
        provider_snapshot=result.provider_snapshot,
        usage=result.usage,
    )


def review_book_direction(
    profile: LlmProfile,
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
) -> tuple[BookDirectionReview, str, dict[str, Any]]:
    result = call_llm(
        profile,
        ChatRequest(
            profile_id=profile.id,
            messages=[
                ChatMessage(role="system", content=_review_system_prompt()),
                ChatMessage(
                    role="user",
                    content=_render_review_context(state, synthesis),
                ),
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            metadata={
                "loop_layer": "book",
                "atomic_action": "review_book_direction",
                "candidate_revision": _next_candidate_revision(state),
                "max_tokens": 3500,
            },
        ),
    )
    payload = _parse_json_object(redact_profile_secrets(result.content, profile))
    issues = _review_issues_from_payload(payload.get("issues"))
    issues.extend(_deterministic_candidate_issues(state, synthesis))
    status: Literal["passed", "blocked"] = (
        "blocked" if any(issue.severity == "blocking" for issue in issues) else "passed"
    )
    signals = _string_list(payload.get("signals"))
    signals.extend(_deterministic_candidate_signals(state, synthesis))
    review = BookDirectionReview(
        status=status,
        summary=_required_string(payload.get("summary"), "summary"),
        issues=issues,
        signals=_dedupe(signals),
    )
    return review, result.model_snapshot, result.usage


def build_review_context_snapshot(state: SetupStateDocument) -> dict[str, Any]:
    synthesis_context = _render_synthesis_context(state)
    version_paths = _discussion_version_paths(state)
    superseded = _json_text(_recent_superseded_payload(state))
    injected_content = [
        ("complete_direction_draft", state.direction_draft, version_paths["direction"]),
        ("discussion_summary", state.discussion_summary, version_paths["state"]),
        ("confirmed_decisions", _json_text(state.confirmed_decisions), version_paths["state"]),
        ("unresolved_questions", _json_text(state.unresolved_questions), version_paths["state"]),
        ("assumptions", _json_text(state.assumptions), version_paths["state"]),
        ("contradictions", _json_text(state.contradictions), version_paths["state"]),
        ("recent_superseded_decisions", superseded, version_paths["state"]),
        ("readiness", state.readiness.model_dump_json(), version_paths["state"]),
    ]
    return {
        "schema_version": 1,
        "loop_layer": "book",
        "atomic_action": "synthesize_and_review_book_direction",
        "state_revision": state.revision,
        "sources": [
            {
                "id": "book-direction-draft",
                "path": "book/direction_draft.md",
                "resolved_version_path": version_paths["direction"],
                "version": state.revision,
                "usage": "direct",
                "included_fields": ["full_text"],
            },
            {
                "id": "book-discussion-state",
                "path": "book/setup.json",
                "resolved_version_path": version_paths["state"],
                "version": state.revision,
                "usage": "direct",
                "included_fields": [
                    "discussion_summary",
                    "confirmed_decisions",
                    "unresolved_questions",
                    "assumptions",
                    "contradictions",
                    "superseded_decisions",
                    "readiness",
                ],
            },
            {
                "id": "book-discussion-transcript",
                "path": "book/discussion/transcript.jsonl",
                "resolved_version_path": version_paths["transcript"],
                "version": state.turn_count,
                "usage": "summary",
            },
        ],
        "injected": [
            _injected_content_record(
                content_id=content_id,
                content=content,
                source_path=source_path,
                version=state.revision,
            )
            for content_id, content, source_path in injected_content
        ],
        "summarized": [
            {
                "id": "complete_discussion_transcript",
                "source_path": version_paths["transcript"],
                "represented_by": "discussion_summary",
            }
        ],
        "excluded": [
            {
                "id": "raw-complete-transcript",
                "reason": "The maintained summary and candidate draft carry the durable intent without replaying every raw turn.",
            },
            {
                "id": "future-story-arcs",
                "reason": "Only the current story arc may be planned after book direction approval.",
            },
        ],
        "budget": {
            "total_character_budget": SYNTHESIS_CONTEXT_CHARACTER_BUDGET,
            "total_character_count": len(synthesis_context),
        },
        "assembly_rationale": (
            "Synthesize the user-visible candidate direction from the maintained draft and compact "
            "discussion state, then review it independently before any approved book artifacts change."
        ),
    }


def _discussion_system_prompt() -> str:
    return """你是 NovelPilot 全书 Loop 的共创访谈模型。你当前只负责和用户深入讨论整本小说的长期方向，不写正文，不规划所有故事弧或章节，也不替用户批准最高层目标。

这是一场开放式讨论：没有固定题库、固定顺序、最低轮数、最高轮数或预设问题数量。先理解并回应用户本轮输入，再根据已有内容聚焦当前少量真正关键的问题，让用户可以自然深入回答，不要一次抛出检查清单。问题必须来自这本具体小说的真实缺口、矛盾或创作机会，禁止机械遍历检查表。

每轮都要重写一份完整、可独立阅读的 Book Direction 草稿。草稿只能把用户明确确认的内容写成确定事实；未确认但有用的推断必须明确标为假设；矛盾和待定项必须保留，不能悄悄替用户决定。模型认为信息充分时，ready_status 可以是 ready，但这只是提示，用户仍可无限继续讨论。

confirmed_decisions 是本轮结束后的完整有效决定列表。不得静默删除已有决定；只有用户本轮明确改变或撤销旧决定时，才能同时在 superseded_decisions 中记录旧决定的完整原文、替代决定、原因，以及来自“用户本轮输入”的逐字短引文。没有被合法取代的旧决定必须继续保留。

suggestions 提供 1 到 3 条用户可能想说的话，每条包含简短 label 和可直接放入输入框的 message；它们只用于启发，不能缩窄自由回答空间。所有用户可见内容使用中文，不输出私有思维链。

严格只返回一个 JSON 对象，字段必须完整：
{
  "reply": "自然回复，并在需要时提出当前真正关键的问题",
  "direction_draft": "完整 Markdown 草稿",
  "discussion_summary": "供后续上下文压缩使用的完整而紧凑的讨论摘要",
  "confirmed_decisions": ["已确认决定"],
  "superseded_decisions": [{"decision":"被取代决定的完整原文","replacement":"替代决定或 null","reason":"取代理由","user_evidence":"用户本轮输入中的逐字短引文"}],
  "unresolved_questions": ["待澄清问题"],
  "assumptions": ["明确标注的假设"],
  "contradictions": ["尚未解决的矛盾"],
  "suggestions": [{"label":"简短标签","message":"用户口吻的候选回复"}],
  "ready_status": "continue 或 ready",
  "readiness_reason": "简短说明"
}"""


def _synthesis_system_prompt() -> str:
    return """你是 NovelPilot 全书 Loop 的方向综合模型。输入是仍属候选状态的开放讨论结果。请把它综合成可供后续故事弧与章节 Harness 使用、同时适合用户审阅的候选全书契约。

不得把待定项或假设伪装成用户确认的事实；不得提前规划整本书的故事弧和章节；不得用模板化空话代替这本具体小说的方向。长期方向应提供稳定约束、读者承诺、冲突与人物演化空间，同时保留滚动创作余地。

constraints.confirmed 和 confirmed_decision_coverage 都必须逐项覆盖输入中的每一条“已确认决定”。constraints.confirmed 必须保留决定原文；coverage 的 decision 必须原文复制，candidate_evidence 必须是候选全书方向或结构化约束中的逐字短引文。不能用笼统说明代替逐项证据。

严格只返回一个 JSON 对象：
{
  "direction_markdown": "完整、具体、可独立审阅的 Markdown 全书方向",
  "constraints": {
    "confirmed": ["明确确认的长期决定"],
    "must_preserve": ["后续必须维护的承诺与边界"],
    "must_avoid": ["写作禁区或禁止方向"],
    "creative_freedoms": ["留给滚动规划的自由空间"],
    "open_decisions": ["仍待用户决定、不得当作事实的事项"]
  },
  "confirmed_decision_coverage": [{"decision":"已确认决定原文","candidate_evidence":"候选文本中的逐字短引文"}],
  "rolling_plan_markdown": "具体说明当前故事弧如何从全书方向启动、后续如何滚动规划和何时回到全书层复核；不得列出未来全部故事弧或章节"
}"""


def _review_system_prompt() -> str:
    return """你是 NovelPilot Harness 的全书方向语义审查模型。你不负责重新创作，只审查候选方向是否忠实承接用户讨论，并给出可路由问题和证据。

只有以下问题可以是 blocking：候选与用户明确决定冲突；把高影响待定项或假设写成事实；内容空泛到无法约束后续故事弧；滚动规划契约缺失；提前写死未来全部故事弧或章节。低影响的不确定性应是 warning，不能用固定题材检查表强迫用户补齐并不需要的信息。

严格只返回一个 JSON 对象，不输出私有思维链：
{
  "summary": "面向用户的简短审查总结",
  "issues": [{
    "severity": "warning 或 blocking",
    "kind": "问题类型",
    "message": "具体问题",
    "evidence": ["来自讨论或候选文本的简短证据"],
    "suggested_question": "需要继续讨论时建议追问的一个问题，或 null"
  }],
  "signals": ["可读验证信号"]
}"""


def _render_synthesis_context(state: SetupStateDocument) -> str:
    context = "\n\n".join(
        [
            "当前完整 Book Direction 草稿：\n" + state.direction_draft,
            "讨论摘要：\n" + state.discussion_summary,
            "已确认决定：\n" + _json_text(state.confirmed_decisions),
            "待澄清问题：\n" + _json_text(state.unresolved_questions),
            "假设：\n" + _json_text(state.assumptions),
            "矛盾：\n" + _json_text(state.contradictions),
            "已被取代的决定：\n"
            + _json_text(_recent_superseded_payload(state)),
            "模型就绪提示（仅供参考）：\n" + state.readiness.model_dump_json(),
        ]
    )
    return _require_context_budget(
        context,
        SYNTHESIS_CONTEXT_CHARACTER_BUDGET,
        "Book direction synthesis context",
    )


def _render_review_context(
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
) -> str:
    context = "\n\n".join(
        [
            "讨论摘要：\n" + state.discussion_summary,
            "用户确认决定：\n" + _json_text(state.confirmed_decisions),
            "讨论中的待定项：\n" + _json_text(state.unresolved_questions),
            "讨论中的假设：\n" + _json_text(state.assumptions),
            "讨论中的矛盾：\n" + _json_text(state.contradictions),
            "候选全书方向：\n" + synthesis.direction_markdown,
            "候选结构化约束：\n" + synthesis.constraints.model_dump_json(indent=2),
            "已确认决定覆盖证据：\n"
            + _json_text(
                [
                    item.model_dump(mode="json")
                    for item in synthesis.confirmed_decision_coverage
                ]
            ),
            "候选滚动规划契约：\n" + synthesis.rolling_plan_markdown,
        ]
    )
    return _require_context_budget(
        context,
        REVIEW_CONTEXT_CHARACTER_BUDGET,
        "Book direction review context",
    )


def _discussion_result_from_payload(
    payload: dict[str, Any],
    result: ChatResult,
    turn: int,
    user_message: str,
) -> BookDiscussionTurnResult:
    ready_status = _required_string(payload.get("ready_status"), "ready_status")
    if ready_status not in {"continue", "ready"}:
        raise ValueError("Book discussion response has an invalid ready_status.")
    validated_ready_status = cast(Literal["continue", "ready"], ready_status)
    turn_result = BookDiscussionTurnResult(
        reply=_required_string(payload.get("reply"), "reply"),
        direction_draft=_required_string(
            payload.get("direction_draft"),
            "direction_draft",
            max_characters=MAX_DIRECTION_DRAFT_CHARACTERS,
        ),
        discussion_summary=_required_string(
            payload.get("discussion_summary"),
            "discussion_summary",
            max_characters=MAX_DISCUSSION_SUMMARY_CHARACTERS,
        ),
        confirmed_decisions=_required_string_list(
            payload.get("confirmed_decisions"), "confirmed_decisions"
        ),
        superseded_decisions=_superseded_decisions_from_payload(
            payload.get("superseded_decisions"),
            turn=turn,
            user_message=user_message,
        ),
        unresolved_questions=_required_string_list(
            payload.get("unresolved_questions"), "unresolved_questions"
        ),
        assumptions=_required_string_list(payload.get("assumptions"), "assumptions"),
        contradictions=_required_string_list(
            payload.get("contradictions"), "contradictions"
        ),
        suggestions=_suggestions_from_payload(payload.get("suggestions"), turn),
        readiness=SetupReadinessSignal(
            status=validated_ready_status,
            reason=_required_string(payload.get("readiness_reason"), "readiness_reason"),
        ),
        model_snapshot=result.model_snapshot,
        provider_snapshot=result.provider_snapshot,
        usage=result.usage,
    )
    decision_state = {
        "confirmed_decisions": turn_result.confirmed_decisions,
        "superseded_decisions": [
            item.model_dump(mode="json") for item in turn_result.superseded_decisions
        ],
        "unresolved_questions": turn_result.unresolved_questions,
        "assumptions": turn_result.assumptions,
        "contradictions": turn_result.contradictions,
    }
    if len(_json_text(decision_state)) > MAX_DECISION_STATE_CHARACTERS:
        raise ValueError("Book discussion decision state exceeds the context budget.")
    return turn_result


def _constraints_from_payload(value: Any) -> BookDirectionConstraints:
    if not isinstance(value, dict):
        raise ValueError("Book direction response is missing constraints.")
    return BookDirectionConstraints(
        confirmed=_required_string_list(value.get("confirmed"), "constraints.confirmed"),
        must_preserve=_required_string_list(
            value.get("must_preserve"), "constraints.must_preserve"
        ),
        must_avoid=_required_string_list(value.get("must_avoid"), "constraints.must_avoid"),
        creative_freedoms=_required_string_list(
            value.get("creative_freedoms"), "constraints.creative_freedoms"
        ),
        open_decisions=_required_string_list(
            value.get("open_decisions"), "constraints.open_decisions"
        ),
    )


def _coverage_from_payload(value: Any) -> list[ConfirmedDecisionCoverage]:
    if not isinstance(value, list):
        raise ValueError("Book direction response is missing confirmed_decision_coverage.")
    coverage: list[ConfirmedDecisionCoverage] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Confirmed decision coverage must be an object.")
        coverage.append(
            ConfirmedDecisionCoverage(
                decision=_required_string(item.get("decision"), "coverage.decision"),
                candidate_evidence=_required_string(
                    item.get("candidate_evidence"),
                    "coverage.candidate_evidence",
                ),
            )
        )
    return coverage


def _superseded_decisions_from_payload(
    value: Any,
    *,
    turn: int,
    user_message: str,
) -> list[SupersededDecision]:
    if not isinstance(value, list):
        raise ValueError("Book discussion response is missing superseded_decisions.")
    decisions: list[SupersededDecision] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Superseded decision must be an object.")
        evidence = _required_string(item.get("user_evidence"), "superseded.user_evidence")
        if evidence not in user_message:
            raise ValueError(
                "Superseded decision evidence must be an exact quote from the current user message."
            )
        replacement = item.get("replacement")
        decisions.append(
            SupersededDecision(
                turn=turn,
                decision=_required_string(item.get("decision"), "superseded.decision"),
                replacement=(
                    replacement.strip()
                    if isinstance(replacement, str) and replacement.strip()
                    else None
                ),
                reason=_required_string(item.get("reason"), "superseded.reason"),
                user_evidence=evidence,
            )
        )
    return decisions


def _suggestions_from_payload(value: Any, turn: int) -> list[SetupSuggestion]:
    if not isinstance(value, list):
        raise ValueError("Book discussion response is missing suggestions.")
    suggestions: list[SetupSuggestion] = []
    for index, item in enumerate(value[:3]):
        if not isinstance(item, dict):
            raise ValueError("Book discussion suggestion must be an object.")
        suggestions.append(
            SetupSuggestion(
                id=f"turn-{turn:04d}-suggestion-{index + 1}",
                label=_required_string(item.get("label"), "suggestion.label"),
                message=_required_string(item.get("message"), "suggestion.message"),
            )
        )
    return suggestions


def _review_issues_from_payload(value: Any) -> list[BookDirectionReviewIssue]:
    if not isinstance(value, list):
        raise ValueError("Book direction review is missing issues.")
    issues: list[BookDirectionReviewIssue] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Book direction review issue must be an object.")
        severity = _required_string(item.get("severity"), "issue.severity")
        if severity not in {"warning", "blocking"}:
            raise ValueError("Book direction review issue has an invalid severity.")
        validated_severity = cast(Literal["warning", "blocking"], severity)
        suggested = item.get("suggested_question")
        issues.append(
            BookDirectionReviewIssue(
                severity=validated_severity,
                kind=_required_string(item.get("kind"), "issue.kind"),
                message=_required_string(item.get("message"), "issue.message"),
                evidence=_required_string_list(item.get("evidence"), "issue.evidence"),
                suggested_question=(
                    suggested.strip() if isinstance(suggested, str) and suggested.strip() else None
                ),
            )
        )
    return issues


def _deterministic_candidate_issues(
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
) -> list[BookDirectionReviewIssue]:
    issues: list[BookDirectionReviewIssue] = []
    if len(synthesis.direction_markdown.strip()) < 300:
        issues.append(
            BookDirectionReviewIssue(
                severity="blocking",
                kind="direction_too_thin",
                message="候选全书方向过于简略，无法稳定约束后续故事弧。",
                evidence=[f"direction_characters:{len(synthesis.direction_markdown.strip())}"],
                suggested_question="这本小说有哪些必须长期兑现、不能被后续创作稀释的具体承诺？",
            )
        )
    if len(synthesis.rolling_plan_markdown.strip()) < 120:
        issues.append(
            BookDirectionReviewIssue(
                severity="blocking",
                kind="rolling_contract_too_thin",
                message="候选滚动规划契约没有说明如何启动并约束当前故事弧。",
                evidence=[f"rolling_contract_characters:{len(synthesis.rolling_plan_markdown.strip())}"],
            )
        )
    constraints = synthesis.constraints
    if not any(
        [
            constraints.confirmed,
            constraints.must_preserve,
            constraints.must_avoid,
            constraints.creative_freedoms,
            constraints.open_decisions,
        ]
    ):
        issues.append(
            BookDirectionReviewIssue(
                severity="blocking",
                kind="empty_constraints",
                message="候选方向没有形成任何可供 Harness 使用的结构化约束。",
                evidence=["all_constraint_groups_empty"],
            )
        )
    covered_decisions = _covered_confirmed_decisions(state, synthesis)
    missing_coverage = [
        decision
        for decision in state.confirmed_decisions
        if decision not in covered_decisions
    ]
    if missing_coverage:
        issues.append(
            BookDirectionReviewIssue(
                severity="blocking",
                kind="confirmed_decision_coverage_missing",
                message="候选方向没有逐项证明其覆盖了全部已确认决定。",
                evidence=missing_coverage,
                suggested_question="哪些已确认决定尚未被候选方向明确承接？",
            )
        )
    missing_structured_decisions = [
        decision
        for decision in state.confirmed_decisions
        if decision not in synthesis.constraints.confirmed
    ]
    if missing_structured_decisions:
        issues.append(
            BookDirectionReviewIssue(
                severity="blocking",
                kind="confirmed_decision_structured_missing",
                message="候选结构化约束没有逐项保留全部已确认决定。",
                evidence=missing_structured_decisions,
                suggested_question="哪些已确认决定尚未进入结构化长期约束？",
            )
        )
    return issues


def _deterministic_candidate_signals(
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
) -> list[str]:
    constraints = synthesis.constraints
    return [
        f"direction_characters:{len(synthesis.direction_markdown.strip())}",
        f"rolling_contract_characters:{len(synthesis.rolling_plan_markdown.strip())}",
        "constraint_items:"
        + str(
            sum(
                len(items)
                for items in [
                    constraints.confirmed,
                    constraints.must_preserve,
                    constraints.must_avoid,
                    constraints.creative_freedoms,
                    constraints.open_decisions,
                ]
            )
        ),
        "confirmed_decision_coverage:"
        + f"{len(_covered_confirmed_decisions(state, synthesis))}/{len(state.confirmed_decisions)}",
    ]


def _next_candidate_revision(state: SetupStateDocument) -> int:
    return state.candidate_revision_counter + 1


def _candidate_search_text(synthesis: BookDirectionSynthesis) -> str:
    constraints = synthesis.constraints
    return "\n".join(
        [
            synthesis.direction_markdown,
            synthesis.rolling_plan_markdown,
            *constraints.confirmed,
            *constraints.must_preserve,
            *constraints.must_avoid,
            *constraints.creative_freedoms,
            *constraints.open_decisions,
        ]
    )


def _covered_confirmed_decisions(
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
) -> set[str]:
    candidate_text = _candidate_search_text(synthesis)
    expected = set(state.confirmed_decisions)
    return {
        item.decision
        for item in synthesis.confirmed_decision_coverage
        if item.decision in expected and item.candidate_evidence.strip() in candidate_text
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("LLM did not return the required JSON object.")


def _required_string(
    value: Any,
    field_name: str,
    *,
    max_characters: int | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"LLM response is missing {field_name}.")
    stripped = value.strip()
    if max_characters is not None and len(stripped) > max_characters:
        raise ValueError(f"LLM response field {field_name} exceeds its context budget.")
    return stripped


def _required_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"LLM response is missing {field_name}.")
    return _dedupe([item.strip() for item in value if isinstance(item, str) and item.strip()])


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe([item.strip() for item in value if isinstance(item, str) and item.strip()])


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _discussion_version_paths(state: SetupStateDocument) -> dict[str, str]:
    return {
        "direction": state.direction_draft_version_path or "book/direction_draft.md",
        "state": state.discussion_state_version_path or "book/setup.json",
        "transcript": (
            state.discussion_transcript_version_path
            or "book/discussion/transcript.jsonl"
        ),
    }


def _recent_superseded_payload(state: SetupStateDocument) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    character_count = 0
    for item in reversed(state.superseded_decisions):
        payload = item.model_dump(mode="json")
        item_count = len(_json_text(payload))
        if character_count + item_count > SUPERSEDED_HISTORY_CHARACTER_BUDGET:
            break
        selected.append(payload)
        character_count += item_count
    return list(reversed(selected))


def _require_context_budget(content: str, budget: int, label: str) -> str:
    if len(content) > budget:
        raise ValueError(f"{label} exceeds the total context budget.")
    return content


def _injected_content_record(
    *,
    content_id: str,
    content: str,
    source_path: str | None,
    version: int,
) -> dict[str, Any]:
    return {
        "id": content_id,
        "source_path": source_path,
        "version": version,
        "character_count": len(content),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
