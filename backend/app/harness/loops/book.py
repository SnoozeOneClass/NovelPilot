import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.llm.gateway import ChatChunk
from app.schemas.profiles import LlmProfile
from app.schemas.setup import (
    BookDirectionConstraints,
    BookDirectionReview,
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
    selected_title: str | None
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
    candidate_artifact_path: str | None = None
    evaluation_record: Any | None = None


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
        ("selected_title", state.selected_title or "尚未确定"),
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
    recent_prompt_payload = [
        {"role": message.role, "content": message.content}
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
            "已确认正式书名：\n" + (state.selected_title or "尚未确定"),
            "近期已取代决定：\n" + _json_text(superseded_payload),
            "最近原始对话：\n" + _json_text(recent_prompt_payload),
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
                    "selected_title",
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
                for content_id, content in fixed_blocks[2:8]
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
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
) -> BookDiscussionTurnResult:
    from app.harness.agents.loop_runners import run_book_discussion_agent

    project_path, metadata, policy = _active_book_agent_context(profile)
    return run_book_discussion_agent(
        project_path,
        metadata,
        state,
        user_message,
        assembly,
        policy,
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )


def synthesize_book_direction(
    profile: LlmProfile,
    state: SetupStateDocument,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
) -> BookDirectionSynthesis:
    from dataclasses import replace

    from app.harness.agents.loop_runners import run_book_direction_agent

    project_path, metadata, policy = _active_book_agent_context(profile)
    synthesis, evaluation, _review = run_book_direction_agent(
        project_path,
        metadata,
        state,
        policy,
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )
    return replace(
        synthesis,
        candidate_artifact_path=evaluation.candidate_artifact_id,
        evaluation_record=evaluation,
    )


def review_book_direction(
    profile: LlmProfile,
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
) -> tuple[BookDirectionReview, str, dict[str, Any]]:
    del on_text_delta
    from app.harness.agents.loop_runners import evaluate_book_direction_candidate
    from app.harness.agents.models import AgentIdentity, EvaluationRecord

    project_path, metadata, policy = _active_book_agent_context(profile)
    if synthesis.evaluation_record is not None:
        evaluation = EvaluationRecord.model_validate(synthesis.evaluation_record)
        from app.harness.agents.loop_runners import _book_review_from_evaluation

        review = _book_review_from_evaluation(evaluation)
    else:
        evaluation, review = evaluate_book_direction_candidate(
            state,
            synthesis,
            policy,
            identity=AgentIdentity(project_id=metadata.project_id, role="book"),
            candidate_path=(
                synthesis.candidate_artifact_path
                or f"book/reviews/review-{_next_candidate_revision(state):04d}/candidate_direction.md"
            ),
            candidate_revision=_next_candidate_revision(state),
        )
    return review, evaluation.evaluator_model_snapshot, {}


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
        ("selected_title", state.selected_title or "尚未确定", version_paths["state"]),
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
                    "selected_title",
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


def _render_synthesis_context(state: SetupStateDocument) -> str:
    context = "\n\n".join(
        [
            "当前完整 Book Direction 草稿：\n" + state.direction_draft,
            "讨论摘要：\n" + state.discussion_summary,
            "已确认决定：\n" + _json_text(state.confirmed_decisions),
            "待澄清问题：\n" + _json_text(state.unresolved_questions),
            "假设：\n" + _json_text(state.assumptions),
            "矛盾：\n" + _json_text(state.contradictions),
            "已确认正式书名：\n" + (state.selected_title or "尚未确定"),
            "已被取代的决定：\n"
            + _json_text(_recent_superseded_payload(state)),
            "模型就绪提示（仅供参考）：\n" + state.readiness.model_dump_json(),
        ]
    )
    return context


def _next_candidate_revision(state: SetupStateDocument) -> int:
    return state.candidate_revision_counter + 1


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


def _active_book_agent_context(
    profile: LlmProfile,
) -> tuple[Any, Any, Any]:
    from app.harness.agents.policy import ResolvedAgentPolicy, resolve_agent_policy
    from app.storage.projects import get_active_project_path, read_project_metadata

    project_path = get_active_project_path()
    if project_path is None:
        raise ValueError("No active project is available for the Book Agent.")
    metadata = read_project_metadata(project_path)
    try:
        policy = resolve_agent_policy(metadata, "book")
    except ValueError:
        configured_ids = {
            metadata.active_profile_id,
            metadata.agent_policy.book_profile_id,
            metadata.agent_policy.evaluator_profile_id,
        }
        if any(item is not None for item in configured_ids):
            raise
        policy = ResolvedAgentPolicy(
            role="book",
            profile=profile,
            evaluator_profile=profile,
            max_turns=metadata.agent_policy.book_max_turns,
            tool_schema_repair_limit=metadata.agent_policy.tool_schema_repair_limit,
            semantic_revision_limit=metadata.agent_policy.semantic_revision_limit,
            transport_retry_limit=metadata.agent_policy.transport_retry_limit,
        )
    return project_path, metadata, policy


def _recent_superseded_payload(state: SetupStateDocument) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    character_count = 0
    for item in reversed(state.superseded_decisions):
        payload = {
            "decision": item.decision,
            "replacement": item.replacement,
            "reason": item.reason,
            "user_evidence": item.user_evidence,
        }
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
