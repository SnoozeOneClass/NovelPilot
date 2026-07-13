import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from app.llm.gateway import ChatChunk, ChatMessage, ChatRequest, ChatResult, call_llm
from app.llm.redaction import redact_profile_secrets
from app.schemas.profiles import LlmProfile
from app.schemas.setup import (
    BookDirectionConstraints,
    BookDirectionReview,
    BookDirectionReviewIssue,
    BookTitleSuggestion,
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
SUPERSEDED_HISTORY_CHARACTER_BUDGET = 8_000
META_PRIORITY_QUESTION_FRAGMENTS = (
    "先讨论哪个",
    "先确认哪个",
    "优先讨论哪个",
    "优先确认哪个",
    "接下来讨论哪个",
    "接下来确认哪个",
    "下一步讨论哪个",
    "下一步确认哪个",
    "选择下一步",
    "选择哪个问题",
    "选择哪个方向",
    "which issue should we",
    "which topic should we",
    "which question should we",
    "what should we discuss first",
    "what should we confirm first",
)


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
    question: str | None
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
    recommended_titles: list[BookTitleSuggestion]
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
    available_recent_characters = RECENT_MESSAGE_CHARACTER_BUDGET
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
            "total_character_budget": None,
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
    on_text_delta: Callable[[ChatChunk], None] | None = None,
) -> BookDiscussionTurnResult:
    result = call_llm(
        profile,
        ChatRequest(
            profile_id=profile.id,
            messages=[
                ChatMessage(role="system", content=_discussion_system_prompt()),
                ChatMessage(role="user", content=assembly.prompt),
            ],
            metadata={
                "loop_layer": "book",
                "atomic_action": "continue_book_discussion",
                "turn": state.turn_count + 1,
                **({"on_text_delta": on_text_delta} if on_text_delta is not None else {}),
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
    on_text_delta: Callable[[ChatChunk], None] | None = None,
) -> BookDirectionSynthesis:
    result = call_llm(
        profile,
        ChatRequest(
            profile_id=profile.id,
            messages=[
                ChatMessage(role="system", content=_synthesis_system_prompt()),
                ChatMessage(role="user", content=_render_synthesis_context(state)),
            ],
            metadata={
                "loop_layer": "book",
                "atomic_action": "synthesize_book_direction",
                "candidate_revision": _next_candidate_revision(state),
                **({"on_text_delta": on_text_delta} if on_text_delta is not None else {}),
            },
        ),
    )
    payload = _parse_json_object(redact_profile_secrets(result.content, profile))
    return BookDirectionSynthesis(
        direction_markdown=_required_string(payload.get("direction_markdown"), "direction_markdown"),
        constraints=_constraints_from_payload(payload.get("constraints")),
        confirmed_decision_coverage=_coverage_from_payload(
            payload.get("confirmed_decision_coverage")
        ),
        recommended_titles=_title_suggestions_from_payload(
            payload.get("recommended_titles")
        ),
        rolling_plan_markdown=_required_string(
            payload.get("rolling_plan_markdown"), "rolling_plan_markdown"
        ),
        model_snapshot=result.model_snapshot,
        provider_snapshot=result.provider_snapshot,
        usage=result.usage,
    )


def review_book_direction(
    profile: LlmProfile,
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
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
            metadata={
                "loop_layer": "book",
                "atomic_action": "review_book_direction",
                "candidate_revision": _next_candidate_revision(state),
                **({"on_text_delta": on_text_delta} if on_text_delta is not None else {}),
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
            "total_character_budget": None,
            "total_character_count": len(synthesis_context),
        },
        "assembly_rationale": (
            "Synthesize the user-visible candidate direction from the maintained draft and compact "
            "discussion state, then review it independently before any approved book artifacts change."
        ),
    }


def _discussion_system_prompt() -> str:
    return """你是 NovelPilot 全书 Loop 的共创访谈模型。你当前只负责和用户深入讨论整本小说的长期方向，不写正文，不规划所有故事弧或章节，也不替用户批准最高层目标。

这是一场开放式讨论：没有固定题库、固定顺序、最低轮数、最高轮数或预设问题数量。先理解并简短回应用户本轮输入，再由你根据完整上下文自主判断并选择当前影响最大的一个未决事项，直接提出关于该事项的具体问题。决定“下一步问什么”是全书 Loop 的职责，不能反过来询问用户想先讨论哪个问题、优先确认哪个方向，也不能把若干讨论主题做成选项让用户选择。每轮最多一个问题，禁止复合提问、连续追问或一次抛出检查清单；其余待定项只保留在 unresolved_questions，留到后续轮次逐一处理。

选择下一问时，优先处理会阻塞大量下游设计的基础口径：人物身份、人数范围、角色关系、对象指代、术语定义和已经出现的硬矛盾；其次是客观规则、时间线与因果链；最后才是在这些基础上展开主题、证据、反转和结局取舍。这只是根据依赖关系判断优先级，不是固定题库。每收到一次用户回答，都要把新信息合并进当前状态，再重新判断下一项最高影响缺口。例如，如果“召集另外六人”和“六名核心人物”造成召集者是否计入六人的歧义，应直接询问召集者是否计入六人，而不是让用户在“澄清人数范围”和其他议题之间选择。

每轮都要重写一份完整、可独立阅读的 Book Direction 草稿。草稿只能把用户明确确认的内容写成确定事实；未确认但有用的推断必须明确标为假设；矛盾和待定项必须保留，不能悄悄替用户决定。模型认为信息充分时，ready_status 可以是 ready，但这只是提示，用户仍可无限继续讨论。

confirmed_decisions 是本轮结束后的完整有效决定列表。不得静默删除已有决定；只有用户本轮明确改变或撤销旧决定时，才能同时在 superseded_decisions 中记录旧决定的完整原文、替代决定、原因，以及来自“用户本轮输入”的逐字短引文。没有被合法取代的旧决定必须继续保留。

当 ready_status 为 continue 时，reply 只写简短的承接和提问理由，不得包含问句；question 必须是你已经选定的唯一一个具体问题，并且只包含一个问号；suggestions 必须提供 2 到 3 个针对这个问题、结合当前上下文且相互有区分度的候选回答。每条 suggestion 必须包含简短 label、可直接作为用户回答的 message、用一句话说明收益或取舍的 rationale，以及布尔值 recommended；每轮必须恰好有一项 recommended 为 true。suggestions 是同一个问题的答案，不是“先讨论什么”“下一步做什么”或“比较哪些方案”的会话动作。不要在 suggestions 中加入“其他”选项，界面会固定提供“自己输入”。当 ready_status 为 ready 时，question 必须为 null，suggestions 必须为空数组。所有用户可见内容使用中文，不输出私有思维链。

严格只返回一个 JSON 对象，字段必须完整：
{
  "reply": "对用户本轮输入的简短承接，以及为什么下一项决定重要；这里不能包含问句",
  "direction_draft": "完整 Markdown 草稿",
  "discussion_summary": "供后续上下文压缩使用的完整而紧凑的讨论摘要",
  "confirmed_decisions": ["已确认决定"],
  "superseded_decisions": [{"decision":"被取代决定的完整原文","replacement":"替代决定或 null","reason":"取代理由","user_evidence":"用户本轮输入中的逐字短引文"}],
  "unresolved_questions": ["待澄清问题"],
  "assumptions": ["明确标注的假设"],
  "contradictions": ["尚未解决的矛盾"],
  "question": "本轮唯一的直接问题；信息充分时为 null",
  "suggestions": [{"label":"简短选项名","message":"可直接提交的用户口吻候选回答","rationale":"一句话说明这个选项的收益或取舍","recommended":true}],
  "ready_status": "continue 或 ready",
  "readiness_reason": "简短说明"
}"""


def _synthesis_system_prompt() -> str:
    return """你是 NovelPilot 全书 Loop 的方向综合模型。输入是仍属候选状态的开放讨论结果。请把它综合成可供后续故事弧与章节 Harness 使用、同时适合用户审阅的候选全书契约。

不得把待定项或假设伪装成用户确认的事实；不得提前规划整本书的故事弧和章节；不得用模板化空话代替这本具体小说的方向。长期方向应提供稳定约束、读者承诺、冲突与人物演化空间，同时保留滚动创作余地。

constraints.confirmed 和 confirmed_decision_coverage 都必须逐项覆盖输入中的每一条“已确认决定”。constraints.confirmed 必须保留决定原文；coverage 的 decision 必须原文复制，candidate_evidence 必须是候选全书方向或结构化约束中的逐字短引文。不能用笼统说明代替逐项证据。

recommended_titles 必须给出 3 至 5 个互不重复的候选书名。每个书名都要结合已经讨论清楚的题材、主角、核心冲突、作品气质和目标读者，并用 rationale 简短说明推荐理由。

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
  "recommended_titles": [{"title":"候选书名","rationale":"结合本书具体方向的推荐理由"}],
  "rolling_plan_markdown": "具体说明当前故事弧如何从全书方向启动、后续如何滚动规划和何时回到全书层复核；不得列出未来全部故事弧或章节"
}"""


def _review_system_prompt() -> str:
    return """你是 NovelPilot Harness 的全书方向语义审查模型。你不负责重新创作，只审查候选方向是否忠实承接用户讨论，并给出可路由问题和证据。

只有以下问题可以是 blocking：候选与用户明确决定冲突；把高影响待定项或假设写成事实；内容空泛到无法约束后续故事弧；滚动规划契约缺失；推荐书名缺失或明显违背已确认方向；提前写死未来全部故事弧或章节。低影响的不确定性应是 warning，不能用固定题材检查表强迫用户补齐并不需要的信息。

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
    return context


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
            "候选书名：\n"
            + _json_text(
                [item.model_dump(mode="json") for item in synthesis.recommended_titles]
            ),
            "候选滚动规划契约：\n" + synthesis.rolling_plan_markdown,
        ]
    )
    return context


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
    reply = _required_string(payload.get("reply"), "reply")
    question = _discussion_question_from_payload(
        payload.get("question"), ready_status=validated_ready_status
    )
    suggestions = _suggestions_from_payload(
        payload.get("suggestions"), turn, ready_status=validated_ready_status
    )
    if "?" in reply or "？" in reply:
        raise ValueError("Book discussion reply must not contain additional questions.")
    turn_result = BookDiscussionTurnResult(
        reply=reply,
        direction_draft=_required_string(payload.get("direction_draft"), "direction_draft"),
        discussion_summary=_required_string(
            payload.get("discussion_summary"), "discussion_summary"
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
        question=question,
        suggestions=suggestions,
        readiness=SetupReadinessSignal(
            status=validated_ready_status,
            reason=_required_string(payload.get("readiness_reason"), "readiness_reason"),
        ),
        model_snapshot=result.model_snapshot,
        provider_snapshot=result.provider_snapshot,
        usage=result.usage,
    )
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


def _title_suggestions_from_payload(value: Any) -> list[BookTitleSuggestion]:
    if not isinstance(value, list) or not 3 <= len(value) <= 5:
        raise ValueError("Book direction response must recommend 3 to 5 titles.")
    suggestions: list[BookTitleSuggestion] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each recommended title must be an object.")
        suggestions.append(BookTitleSuggestion.model_validate(item))
    normalized = [item.title.casefold() for item in suggestions]
    if len(normalized) != len(set(normalized)):
        raise ValueError("Recommended book titles must be unique.")
    return suggestions


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


def _discussion_question_from_payload(
    value: Any,
    *,
    ready_status: Literal["continue", "ready"],
) -> str | None:
    if ready_status == "ready":
        if value is not None:
            raise ValueError("Ready book discussion response must not contain a question.")
        return None
    question = _required_string(value, "question")
    if question.count("?") + question.count("？") != 1:
        raise ValueError("Book discussion response must contain exactly one question.")
    if not question.endswith(("?", "？")):
        raise ValueError("Book discussion question must end with a question mark.")
    normalized = question.casefold()
    if any(fragment in normalized for fragment in META_PRIORITY_QUESTION_FRAGMENTS):
        raise ValueError(
            "Book discussion must choose the next concrete decision instead of "
            "delegating topic prioritization to the user."
        )
    return question


def _suggestions_from_payload(
    value: Any,
    turn: int,
    *,
    ready_status: Literal["continue", "ready"],
) -> list[SetupSuggestion]:
    if not isinstance(value, list):
        raise ValueError("Book discussion response is missing suggestions.")
    if ready_status == "ready":
        if value:
            raise ValueError("Ready book discussion response must not contain suggestions.")
        return []
    if not 2 <= len(value) <= 3:
        raise ValueError("Book discussion response must provide 2 to 3 answer options.")
    suggestions: list[SetupSuggestion] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError("Book discussion suggestion must be an object.")
        suggestions.append(
            SetupSuggestion(
                id=f"turn-{turn:04d}-suggestion-{index + 1}",
                label=_required_string(item.get("label"), "suggestion.label"),
                message=_required_string(item.get("message"), "suggestion.message"),
                rationale=_required_string(
                    item.get("rationale"), "suggestion.rationale"
                ),
                recommended=_suggestion_recommended_from_payload(
                    item.get("recommended")
                ),
            )
        )
    labels = [item.label.casefold() for item in suggestions]
    messages = [item.message.casefold() for item in suggestions]
    if len(labels) != len(set(labels)) or len(messages) != len(set(messages)):
        raise ValueError("Book discussion answer options must be unique.")
    if sum(item.recommended for item in suggestions) != 1:
        raise ValueError(
            "Book discussion response must recommend exactly one answer option."
        )
    return suggestions


def _suggestion_recommended_from_payload(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Book discussion suggestion.recommended must be a boolean.")
    return value


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
        f"recommended_titles:{len(synthesis.recommended_titles)}",
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
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"LLM response is missing {field_name}.")
    return value.strip()


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
