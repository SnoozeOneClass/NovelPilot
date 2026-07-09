from datetime import UTC, datetime
from pathlib import Path

from app.schemas.setup import (
    SetupAnswer,
    SetupAnswerRequest,
    SetupOption,
    SetupQuestion,
    SetupStateDocument,
)
from app.storage.json_files import read_json, write_json
from app.storage.text_files import write_text_file


DEFAULT_SETUP_QUESTIONS = [
    SetupQuestion(
        id="genre_promise",
        title="Genre Promise",
        prompt="What kind of reader promise should this novel keep?",
        options=[
            SetupOption(
                id="tense_mystery",
                label="Tense mystery",
                description="A suspense-driven story with clues, reversals, and a clear reveal path.",
            ),
            SetupOption(
                id="character_growth",
                label="Character growth",
                description="A relationship-forward story where inner change carries the main payoff.",
            ),
            SetupOption(
                id="epic_adventure",
                label="Epic adventure",
                description="A broad journey with escalating stakes, places, factions, and discoveries.",
            ),
        ],
    ),
    SetupQuestion(
        id="protagonist_direction",
        title="Protagonist Direction",
        prompt="What long-term direction should the protagonist move toward?",
        options=[
            SetupOption(
                id="recover_agency",
                label="Recover agency",
                description="The protagonist starts constrained and gradually becomes a decisive actor.",
            ),
            SetupOption(
                id="pay_a_cost",
                label="Pay a cost",
                description="The protagonist gains what they want but must give up something meaningful.",
            ),
            SetupOption(
                id="change_belief",
                label="Change belief",
                description="The protagonist's core worldview is challenged and reshaped by the plot.",
            ),
        ],
    ),
    SetupQuestion(
        id="world_constraints",
        title="World Constraints",
        prompt="Which world constraints must the harness preserve while writing?",
        options=[
            SetupOption(
                id="low_magic",
                label="Low magic",
                description="Unusual events exist, but they stay rare, costly, and consequential.",
            ),
            SetupOption(
                id="grounded_modern",
                label="Grounded modern",
                description="The world follows present-day social, technical, and physical constraints.",
            ),
            SetupOption(
                id="political_factions",
                label="Political factions",
                description="Power groups have visible interests and must react consistently over time.",
            ),
        ],
    ),
    SetupQuestion(
        id="reader_promise",
        title="Reader Promise",
        prompt="What should readers reliably get from each story arc?",
        options=[
            SetupOption(
                id="emotional_turn",
                label="Emotional turn",
                description="Each arc changes what a key relationship or self-belief means.",
            ),
            SetupOption(
                id="strategic_win",
                label="Strategic win",
                description="Each arc resolves a concrete tactical problem and opens a sharper one.",
            ),
            SetupOption(
                id="revelation",
                label="Revelation",
                description="Each arc reveals a truth that changes how earlier events are understood.",
            ),
        ],
    ),
    SetupQuestion(
        id="ending_tendency",
        title="Ending Tendency",
        prompt="What ending tendency should guide long-term decisions without prewriting the whole book?",
        options=[
            SetupOption(
                id="hopeful",
                label="Hopeful",
                description="The ending should feel earned, difficult, and ultimately restorative.",
            ),
            SetupOption(
                id="bittersweet",
                label="Bittersweet",
                description="The ending should grant meaning or victory while preserving real loss.",
            ),
            SetupOption(
                id="tragic",
                label="Tragic",
                description="The ending should fulfill the premise through irreversible consequence.",
            ),
        ],
    ),
]


def initialize_setup_state(project_path: Path) -> SetupStateDocument:
    state = _new_setup_state()
    _write_setup_state(project_path, state)
    return state


def read_setup_state(project_path: Path) -> SetupStateDocument:
    data = read_json(_setup_path(project_path))
    if data is None:
        return initialize_setup_state(project_path)

    state = SetupStateDocument.model_validate(data)
    state.questions = _merge_stored_questions(state.questions)
    state.next_question = _next_unanswered_question(state)
    if state.approved:
        state.next_question = None
        state.ready_for_approval = True
    elif state.next_question is not None:
        _clear_readiness_assessment(state)
    return state


def answer_setup_question(
    project_path: Path,
    request: SetupAnswerRequest,
) -> SetupStateDocument:
    state = read_setup_state(project_path)
    if state.approved:
        raise ValueError("Book setup is already approved.")

    question_ids = {question.id for question in state.questions}
    if request.question_id not in question_ids:
        raise ValueError(f"Unknown setup question: {request.question_id}")

    answer = SetupAnswer(
        question_id=request.question_id,
        answer=request.answer,
    )
    state.answers = [
        existing for existing in state.answers if existing.question_id != request.question_id
    ]
    state.answers.append(answer)
    _clear_readiness_assessment(state)
    state.next_question = _next_unanswered_question(state)
    _write_setup_state(project_path, state)
    return state


def approve_setup(project_path: Path) -> SetupStateDocument:
    state = read_setup_state(project_path)
    if state.approved:
        return state

    missing = _missing_required_question_ids(state)
    if missing:
        raise ValueError(f"Book setup is missing required answers: {', '.join(missing)}")

    state.approved = True
    state.approved_at = datetime.now(UTC)
    state.ready_for_approval = True
    if state.readiness_assessed_at is None:
        state.readiness_assessed_at = state.approved_at
    state.next_question = None
    _write_setup_state(project_path, state)
    _write_approved_book_artifacts(project_path, state)
    return state


def replace_setup_question(
    project_path: Path,
    state: SetupStateDocument,
    question: SetupQuestion,
) -> SetupStateDocument:
    state.questions = [
        question if existing.id == question.id else existing
        for existing in state.questions
    ]
    state.next_question = _next_unanswered_question(state)
    _write_setup_state(project_path, state)
    return state


def append_setup_question(
    project_path: Path,
    state: SetupStateDocument,
    question: SetupQuestion,
) -> SetupStateDocument:
    if any(existing.id == question.id for existing in state.questions):
        raise ValueError(f"Setup question already exists: {question.id}")
    state.questions.append(question)
    _clear_readiness_assessment(state)
    state.next_question = _next_unanswered_question(state)
    _write_setup_state(project_path, state)
    return state


def mark_ready_for_approval(
    project_path: Path,
    state: SetupStateDocument,
    profile_id: str | None = None,
) -> SetupStateDocument:
    state.next_question = _next_unanswered_question(state)
    if state.next_question is not None:
        raise ValueError("Book setup still has unanswered required questions.")
    state.ready_for_approval = True
    state.readiness_assessed_at = datetime.now(UTC)
    state.readiness_profile_id = profile_id
    _write_setup_state(project_path, state)
    return state


def _new_setup_state() -> SetupStateDocument:
    state = SetupStateDocument(questions=DEFAULT_SETUP_QUESTIONS)
    state.next_question = _next_unanswered_question(state)
    return state


def _clear_readiness_assessment(state: SetupStateDocument) -> None:
    state.ready_for_approval = False
    state.readiness_assessed_at = None
    state.readiness_profile_id = None


def _merge_stored_questions(stored_questions: list[SetupQuestion]) -> list[SetupQuestion]:
    stored_by_id = {question.id: question for question in stored_questions}
    default_ids = {question.id for question in DEFAULT_SETUP_QUESTIONS}
    merged_defaults = [
        stored_by_id.get(default_question.id, default_question)
        for default_question in DEFAULT_SETUP_QUESTIONS
    ]
    stored_followups = [
        question for question in stored_questions if question.id not in default_ids
    ]
    return merged_defaults + stored_followups


def _setup_path(project_path: Path) -> Path:
    return project_path / "book" / "setup.json"


def _write_setup_state(project_path: Path, state: SetupStateDocument) -> None:
    write_json(_setup_path(project_path), state.model_dump(mode="json"))


def _next_unanswered_question(state: SetupStateDocument) -> SetupQuestion | None:
    answered = {
        answer.question_id for answer in state.answers if answer.answer.strip()
    }
    for question in state.questions:
        if question.required and question.id not in answered:
            return question
    return None


def _missing_required_question_ids(state: SetupStateDocument) -> list[str]:
    answered = {
        answer.question_id for answer in state.answers if answer.answer.strip()
    }
    return [
        question.id
        for question in state.questions
        if question.required and question.id not in answered
    ]


def _answers_by_question(state: SetupStateDocument) -> dict[str, SetupAnswer]:
    return {answer.question_id: answer for answer in state.answers}


def _write_approved_book_artifacts(project_path: Path, state: SetupStateDocument) -> None:
    answers = _answers_by_question(state)
    write_text_file(
        project_path / "book" / "settings.md",
        _render_settings_markdown(state, answers),
    )
    write_text_file(project_path / "book" / "outline.md", _render_outline_markdown())

    previous_state = read_json(project_path / "book" / "state.json", default={}) or {}
    previous_version = int(previous_state.get("version", 1))
    write_json(
        project_path / "book" / "state.json",
        {
            "schema_version": 1,
            "version": previous_version + 1,
            "setup_approved": True,
            "approved_at": state.approved_at.isoformat() if state.approved_at else None,
            "answers": {
                question.id: answers[question.id].answer
                for question in state.questions
                if question.id in answers
            },
            "current_strategy": "rolling_story_arc_planning",
        },
    )


def _render_settings_markdown(
    state: SetupStateDocument,
    answers: dict[str, SetupAnswer],
) -> str:
    lines = ["# Book Settings", ""]
    for question in state.questions:
        answer = answers.get(question.id)
        if answer is None:
            continue
        lines.extend([f"## {question.title}", "", answer.answer, ""])
    return "\n".join(lines).rstrip() + "\n"


def _render_outline_markdown() -> str:
    return "\n".join(
        [
            "# Book Outline",
            "",
            "The book uses rolling story arc planning.",
            "Only the current arc is planned from committed state, feedback, and book direction.",
            "",
        ]
    )
