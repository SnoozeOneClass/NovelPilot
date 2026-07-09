import pytest
from pydantic import SecretStr, ValidationError

from app.harness.loops import book as book_loop
from app.llm.gateway import ChatResult
from app.schemas.profiles import LlmProfile
from app.schemas.setup import SetupAnswerRequest
from app.schemas.setup import SetupAnswer
from app.schemas.setup import SetupOption
from app.schemas.setup import SetupQuestion
from app.schemas.projects import ProjectMetadata
from app.storage.events import read_events
from app.storage.json_files import read_json
from app.storage.json_files import write_json
from app.storage.setup import (
    DEFAULT_SETUP_QUESTIONS,
    answer_setup_question,
    append_setup_question,
    approve_setup,
    initialize_setup_state,
    read_setup_state,
    replace_setup_question,
)


def test_setup_state_initializes_with_first_question(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)

    state = initialize_setup_state(project_path)

    assert state.approved is False
    assert state.next_question is not None
    assert state.next_question.id == DEFAULT_SETUP_QUESTIONS[0].id
    assert (project_path / "book" / "setup.json").exists()


def test_answer_setup_question_moves_to_next_required_question(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)

    state = answer_setup_question(
        project_path,
        SetupAnswerRequest(question_id="genre_promise", answer="A tense mystery."),
    )

    assert state.answers[0].answer == "A tense mystery."
    assert state.next_question is not None
    assert state.next_question.id == "protagonist_direction"


def test_setup_answer_request_rejects_blank_answer() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        SetupAnswerRequest(question_id="genre_promise", answer="   ")


def test_blank_setup_answer_does_not_satisfy_required_question(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    state = initialize_setup_state(project_path)
    state.answers = [SetupAnswer(question_id="genre_promise", answer="   ")]
    write_json(project_path / "book" / "setup.json", state.model_dump(mode="json"))

    reloaded = read_setup_state(project_path)

    assert reloaded.next_question is not None
    assert reloaded.next_question.id == "genre_promise"
    with pytest.raises(ValueError, match="genre_promise"):
        approve_setup(project_path)


def test_approve_setup_requires_all_required_answers(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)

    with pytest.raises(ValueError, match="missing required answers"):
        approve_setup(project_path)


def test_approve_setup_writes_book_artifacts(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    (project_path / "book" / "state.json").write_text(
        '{"schema_version": 1, "version": 1}\n',
        encoding="utf-8",
    )

    for question in DEFAULT_SETUP_QUESTIONS:
        answer_setup_question(
            project_path,
            SetupAnswerRequest(question_id=question.id, answer=f"Answer for {question.id}."),
        )

    state = approve_setup(project_path)
    book_state = read_json(project_path / "book" / "state.json")
    settings = (project_path / "book" / "settings.md").read_text(encoding="utf-8")

    assert state.approved is True
    assert state.next_question is None
    assert book_state["version"] == 2
    assert book_state["setup_approved"] is True
    assert "## Genre Promise" in settings


def test_replace_setup_question_persists_personalized_question(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    state = initialize_setup_state(project_path)
    assert state.next_question is not None

    question = SetupQuestion(
        id=state.next_question.id,
        title="A sharper promise",
        prompt="Which sharper promise should the book make?",
        options=[
            SetupOption(id="a", label="A", description="First option."),
            SetupOption(id="b", label="B", description="Second option."),
            SetupOption(id="c", label="C", description="Third option."),
        ],
        source="llm",
        profile_id="main",
        model_snapshot="story-model",
    )

    replace_setup_question(project_path, state, question)
    reloaded = read_setup_state(project_path)

    assert reloaded.next_question is not None
    assert reloaded.next_question.title == "A sharper promise"
    assert reloaded.next_question.source == "llm"
    assert reloaded.next_question.profile_id == "main"


def test_read_setup_state_preserves_llm_followup_questions(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    for question in DEFAULT_SETUP_QUESTIONS:
        state = answer_setup_question(
            project_path,
            SetupAnswerRequest(question_id=question.id, answer=f"Answer for {question.id}."),
        )

    followup = SetupQuestion(
        id="llm_followup_001",
        title="Missing Constraint",
        prompt="Which extra constraint matters most?",
        options=[
            SetupOption(id="a", label="A", description="First option."),
            SetupOption(id="b", label="B", description="Second option."),
            SetupOption(id="c", label="C", description="Third option."),
        ],
        source="llm",
        profile_id="main",
        model_snapshot="story-model",
    )

    append_setup_question(project_path, state, followup)
    reloaded = read_setup_state(project_path)

    assert reloaded.questions[-1].id == "llm_followup_001"
    assert reloaded.next_question is not None
    assert reloaded.next_question.id == "llm_followup_001"


def test_book_loop_personalizes_setup_question_from_llm(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    state = initialize_setup_state(project_path)
    assert state.next_question is not None
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )

    monkeypatch.setattr(
        book_loop,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content=(
                '{"title":"Promise Focus","prompt":"Which promise should dominate?",'
                '"options":['
                '{"label":"Puzzle heat","description":"Keep every arc clue-forward."},'
                '{"label":"Emotional cost","description":"Make every win hurt personally."},'
                '{"label":"Danger ladder","description":"Escalate visible external risk."}'
                "]}"
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    question = book_loop.personalize_next_setup_question(profile, state, state.next_question)

    assert question.id == state.next_question.id
    assert question.title == "Promise Focus"
    assert question.source == "llm"
    assert question.profile_id == "main"
    assert question.model_snapshot == "story-model"
    assert [option.label for option in question.options] == [
        "Puzzle heat",
        "Emotional cost",
        "Danger ladder",
    ]


def test_book_loop_can_request_setup_followup_question(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    for question in DEFAULT_SETUP_QUESTIONS:
        state = answer_setup_question(
            project_path,
            SetupAnswerRequest(question_id=question.id, answer=f"Answer for {question.id}."),
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )

    monkeypatch.setattr(
        book_loop,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content=(
                '{"status":"needs_more_info","question":{'
                '"title":"Conflict Boundary",'
                '"prompt":"Which conflict boundary should the harness preserve?",'
                '"options":['
                '{"label":"Personal","description":"Keep conflict personally grounded."},'
                '{"label":"Political","description":"Keep factions visibly reactive."},'
                '{"label":"Mystery","description":"Keep clues fair and inspectable."}'
                "]}}"
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    followup = book_loop.assess_setup_followup_question(profile, state)

    assert followup is not None
    assert followup.id == "llm_followup_001"
    assert followup.title == "Conflict Boundary"
    assert followup.source == "llm"
    assert [option.label for option in followup.options] == ["Personal", "Political", "Mystery"]


def test_setup_api_personalizes_next_question_after_answer(tmp_path, monkeypatch) -> None:
    from app.api import setup as setup_api

    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )

    def fake_personalize(_profile, _state, question):
        return SetupQuestion(
            id=question.id,
            title="Personalized protagonist question",
            prompt="What should change because of the mystery promise?",
            options=[
                SetupOption(id="agency", label="Agency", description="Gain agency."),
                SetupOption(id="cost", label="Cost", description="Pay a cost."),
                SetupOption(id="belief", label="Belief", description="Change belief."),
            ],
            source="llm",
            profile_id="main",
            model_snapshot="story-model",
        )

    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)
    monkeypatch.setattr(setup_api, "personalize_next_setup_question", fake_personalize)

    state = setup_api.answer_setup_question(
        SetupAnswerRequest(question_id="genre_promise", answer="Tense mystery.")
    )
    events = read_events(project_path)

    assert state.next_question is not None
    assert state.next_question.id == "protagonist_direction"
    assert state.next_question.title == "Personalized protagonist question"
    assert state.next_question.source == "llm"
    assert events[-1].kind == "setup_question_personalized"


def test_setup_api_redacts_question_personalization_failure_event(
    tmp_path,
    monkeypatch,
) -> None:
    from app.api import setup as setup_api

    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret-key"),
        model="story-model",
    )

    def fail_personalize(_profile, _state, _question):
        raise RuntimeError("provider echoed secret-key at https://api.example.com/v1")

    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)
    monkeypatch.setattr(setup_api, "personalize_next_setup_question", fail_personalize)

    state = setup_api.answer_setup_question(
        SetupAnswerRequest(question_id="genre_promise", answer="Tense mystery.")
    )
    payload = "\n".join(str(event.payload) for event in read_events(project_path))

    assert state.next_question is not None
    assert state.next_question.id == "protagonist_direction"
    assert "secret-key" not in payload
    assert "https://api.example.com/v1" not in payload
    assert "[redacted]" in payload


def test_setup_api_adds_followup_after_required_answers(tmp_path, monkeypatch) -> None:
    from app.api import setup as setup_api

    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )

    def fake_followup(_profile, _state):
        return SetupQuestion(
            id="llm_followup_001",
            title="Conflict Boundary",
            prompt="Which conflict boundary should the harness preserve?",
            options=[
                SetupOption(id="personal", label="Personal", description="Personal stakes."),
                SetupOption(id="political", label="Political", description="Faction stakes."),
                SetupOption(id="mystery", label="Mystery", description="Fair clues."),
            ],
            source="llm",
            profile_id="main",
            model_snapshot="story-model",
        )

    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        setup_api,
        "personalize_next_setup_question",
        lambda _profile, _state, question: question,
    )
    monkeypatch.setattr(setup_api, "assess_setup_followup_question", fake_followup)

    state = read_setup_state(project_path)
    for question in DEFAULT_SETUP_QUESTIONS:
        state = setup_api.answer_setup_question(
            SetupAnswerRequest(question_id=question.id, answer=f"Answer for {question.id}.")
        )
    events = read_events(project_path)

    assert state.next_question is not None
    assert state.next_question.id == "llm_followup_001"
    assert state.next_question.source == "llm"
    assert events[-1].kind == "setup_followup_question_created"


def test_setup_api_approve_assesses_followup_after_profile_is_selected(
    tmp_path,
    monkeypatch,
) -> None:
    from app.api import setup as setup_api

    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    for question in DEFAULT_SETUP_QUESTIONS:
        answer_setup_question(
            project_path,
            SetupAnswerRequest(question_id=question.id, answer=f"Answer for {question.id}."),
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )

    def fake_followup(_profile, _state):
        return SetupQuestion(
            id="llm_followup_001",
            title="New Profile Constraint",
            prompt="Which constraint should the selected model preserve?",
            options=[
                SetupOption(id="tone", label="Tone", description="Preserve tone."),
                SetupOption(id="logic", label="Logic", description="Preserve logic."),
                SetupOption(id="pacing", label="Pacing", description="Preserve pacing."),
            ],
            source="llm",
            profile_id="main",
            model_snapshot="story-model",
        )

    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)
    monkeypatch.setattr(setup_api, "assess_setup_followup_question", fake_followup)

    state = setup_api.approve_setup()
    events = read_events(project_path)

    assert state.approved is False
    assert state.next_question is not None
    assert state.next_question.id == "llm_followup_001"
    assert not (project_path / "book" / "settings.md").exists()
    assert events[-1].kind == "setup_followup_question_created"


def test_setup_api_allows_multiple_llm_followups_before_approval(
    tmp_path,
    monkeypatch,
) -> None:
    from app.api import setup as setup_api

    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    followups = [
        SetupQuestion(
            id="llm_followup_001",
            title="Conflict Boundary",
            prompt="Which conflict boundary matters most?",
            options=[
                SetupOption(id="personal", label="Personal", description="Personal stakes."),
                SetupOption(id="political", label="Political", description="Faction stakes."),
                SetupOption(id="mystery", label="Mystery", description="Fair clues."),
            ],
            source="llm",
            profile_id="main",
            model_snapshot="story-model",
        ),
        SetupQuestion(
            id="llm_followup_002",
            title="Narrative Texture",
            prompt="Which texture should later chapters preserve?",
            options=[
                SetupOption(id="spare", label="Spare", description="Lean and tense."),
                SetupOption(id="lyrical", label="Lyrical", description="Image-rich prose."),
                SetupOption(id="wry", label="Wry", description="Dry observational humor."),
            ],
            source="llm",
            profile_id="main",
            model_snapshot="story-model",
        ),
        None,
    ]

    def fake_followup(_profile, _state):
        return followups.pop(0)

    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        setup_api,
        "personalize_next_setup_question",
        lambda _profile, _state, question: question,
    )
    monkeypatch.setattr(setup_api, "assess_setup_followup_question", fake_followup)

    state = read_setup_state(project_path)
    for question in DEFAULT_SETUP_QUESTIONS:
        state = setup_api.answer_setup_question(
            SetupAnswerRequest(question_id=question.id, answer=f"Answer for {question.id}.")
        )
    assert state.next_question is not None
    assert state.next_question.id == "llm_followup_001"

    state = setup_api.answer_setup_question(
        SetupAnswerRequest(question_id="llm_followup_001", answer="Keep conflict personal.")
    )
    assert state.next_question is not None
    assert state.next_question.id == "llm_followup_002"

    state = setup_api.answer_setup_question(
        SetupAnswerRequest(question_id="llm_followup_002", answer="Keep the prose spare.")
    )
    assert state.next_question is None
    assert state.ready_for_approval is True
    assert state.readiness_profile_id == "main"

    monkeypatch.setattr(
        setup_api,
        "assess_setup_followup_question",
        lambda _profile, _state: pytest.fail("readiness should not be reassessed"),
    )
    approved = setup_api.approve_setup()

    assert approved.approved is True
    assert approved.ready_for_approval is True
    assert followups == []


def test_setup_api_redacts_followup_assessment_failure_event(
    tmp_path,
    monkeypatch,
) -> None:
    from app.api import setup as setup_api

    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    initialize_setup_state(project_path)
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    for question in DEFAULT_SETUP_QUESTIONS:
        answer_setup_question(
            project_path,
            SetupAnswerRequest(question_id=question.id, answer=f"Answer for {question.id}."),
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret-key"),
        model="story-model",
    )

    def fail_followup(_profile, _state):
        raise RuntimeError("provider echoed secret-key at https://api.example.com/v1")

    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)
    monkeypatch.setattr(setup_api, "assess_setup_followup_question", fail_followup)

    state = setup_api.approve_setup()
    payload = "\n".join(str(event.payload) for event in read_events(project_path))

    assert state.approved is True
    assert "secret-key" not in payload
    assert "https://api.example.com/v1" not in payload
    assert "[redacted]" in payload
