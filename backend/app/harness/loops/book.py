import json
import re
from typing import Any

from app.llm.gateway import ChatMessage, ChatRequest, call_llm
from app.schemas.profiles import LlmProfile
from app.schemas.setup import SetupOption, SetupQuestion, SetupStateDocument


class BookLoop:
    """Owns book-level setup and long-term direction."""


FOLLOWUP_FALLBACK_OPTIONS = [
    SetupOption(
        id="tighten_reader_promise",
        label="Tighten promise",
        description="Clarify what the reader should reliably feel or discover.",
    ),
    SetupOption(
        id="tighten_constraint",
        label="Tighten constraint",
        description="Add a boundary the harness must preserve while writing.",
    ),
    SetupOption(
        id="tighten_character",
        label="Tighten character",
        description="Clarify a protagonist pressure, flaw, or desired change.",
    ),
]


def personalize_next_setup_question(
    profile: LlmProfile,
    state: SetupStateDocument,
    question: SetupQuestion,
) -> SetupQuestion:
    result = call_llm(
        profile,
        ChatRequest(
            profile_id=profile.id,
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "You are Novelpilot's book-loop setup interviewer. "
                        "Generate visible user-facing setup questions only. "
                        "Do not include private chain-of-thought."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=_render_setup_question_prompt(state, question),
                ),
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
            metadata={
                "loop_layer": "book",
                "atomic_action": "personalize_setup_question",
                "question_id": question.id,
                "max_tokens": 1200,
            },
        ),
    )
    payload = _parse_json_object(result.content)
    generated = _question_from_payload(question, payload)
    generated.source = "llm"
    generated.profile_id = profile.id
    generated.model_snapshot = result.model_snapshot
    return generated


def assess_setup_followup_question(
    profile: LlmProfile,
    state: SetupStateDocument,
) -> SetupQuestion | None:
    result = call_llm(
        profile,
        ChatRequest(
            profile_id=profile.id,
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "You are Novelpilot's book-loop setup interviewer. Decide whether "
                        "one more user-facing decision is needed before approval. Do not "
                        "include private chain-of-thought."
                    ),
                ),
                ChatMessage(role="user", content=_render_followup_prompt(state)),
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            metadata={
                "loop_layer": "book",
                "atomic_action": "assess_setup_readiness",
                "max_tokens": 1200,
            },
        ),
    )
    payload = _parse_json_object(result.content)
    if payload.get("status") != "needs_more_info":
        return None

    question_payload = payload.get("question")
    if not isinstance(question_payload, dict):
        return None

    fallback = SetupQuestion(
        id=_next_followup_question_id(state),
        title="Additional Book Decision",
        prompt="Which additional constraint should guide the novel before approval?",
        options=FOLLOWUP_FALLBACK_OPTIONS,
    )
    generated = _question_from_payload(fallback, question_payload)
    generated.source = "llm"
    generated.profile_id = profile.id
    generated.model_snapshot = result.model_snapshot
    return generated


def _render_setup_question_prompt(
    state: SetupStateDocument,
    question: SetupQuestion,
) -> str:
    prior_answers = [
        {
            "question_id": answer.question_id,
            "answer": answer.answer,
        }
        for answer in state.answers
    ]
    base_options = [
        {
            "id": option.id,
            "label": option.label,
            "description": option.description,
        }
        for option in question.options
    ]
    return "\n\n".join(
        [
            "The user is configuring a local single-user long-form novel project.",
            "Use the prior answers to adapt the next book-level decision.",
            "Keep the same question id and decision domain. Ask exactly one decision.",
            "Return strict JSON with keys: title, prompt, options.",
            "options must contain exactly three objects with label and description. "
            "The UI already provides a custom-answer path, so do not add an Other option.",
            f"Next decision id: {question.id}",
            f"Default title: {question.title}",
            f"Default prompt: {question.prompt}",
            f"Default options: {json.dumps(base_options, ensure_ascii=False)}",
            f"Prior answers: {json.dumps(prior_answers, ensure_ascii=False)}",
        ]
    )


def _render_followup_prompt(state: SetupStateDocument) -> str:
    answers = [
        {"question_id": answer.question_id, "answer": answer.answer}
        for answer in state.answers
    ]
    answered_question_ids = {answer.question_id for answer in state.answers}
    already_asked = [
        {
            "id": question.id,
            "title": question.title,
            "prompt": question.prompt,
        }
        for question in state.questions
        if question.id in answered_question_ids
    ]
    return "\n\n".join(
        [
            "The user has answered the current book setup decisions for a local long-form novel.",
            "Decide whether one more essential decision is needed before book-loop approval.",
            "Ask only if the missing information would materially affect long-term writing stability.",
            "Return strict JSON in one of these forms:",
            '{"status":"ready"}',
            (
                '{"status":"needs_more_info","question":{"title":"...","prompt":"...",'
                '"options":[{"label":"...","description":"..."},{"label":"...",'
                '"description":"..."},{"label":"...","description":"..."}]}}'
            ),
            "The UI already provides a custom-answer path; do not add an Other option.",
            f"Answered questions: {json.dumps(already_asked, ensure_ascii=False)}",
            f"Answers: {json.dumps(answers, ensure_ascii=False)}",
        ]
    )


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
    raise ValueError("LLM did not return a JSON object for the setup question.")


def _question_from_payload(
    fallback: SetupQuestion,
    payload: dict[str, Any],
) -> SetupQuestion:
    title = _required_string(payload.get("title"), fallback.title)
    prompt = _required_string(payload.get("prompt"), fallback.prompt)
    options = _options_from_payload(payload.get("options"), fallback.options)
    return SetupQuestion(
        id=fallback.id,
        title=title,
        prompt=prompt,
        options=options,
        required=fallback.required,
    )


def _options_from_payload(value: Any, fallback: list[SetupOption]) -> list[SetupOption]:
    if not isinstance(value, list):
        return fallback

    options: list[SetupOption] = []
    for index, item in enumerate(value[:3]):
        if not isinstance(item, dict):
            continue
        label = _required_string(item.get("label"), "")
        description = _required_string(item.get("description"), "")
        if not label or not description:
            continue
        raw_id_value = item.get("id")
        raw_id = raw_id_value if isinstance(raw_id_value, str) else label
        options.append(
            SetupOption(
                id=_option_id(raw_id, index),
                label=label,
                description=description,
            )
        )
    return options if len(options) == 3 else fallback


def _required_string(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _option_id(value: str, index: int) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip().lower()).strip("_")
    return slug or f"option_{index + 1}"


def _next_followup_question_id(state: SetupStateDocument) -> str:
    existing_ids = {question.id for question in state.questions}
    index = 1
    while True:
        question_id = f"llm_followup_{index:03d}"
        if question_id not in existing_ids:
            return question_id
        index += 1
