from pydantic import SecretStr

from app.llm.gateway import ChatMessage, ChatRequest, ChatResult
from app.llm.provider_errors import ProviderCallError
from app.llm.retry import call_llm_with_transport_retries
from app.schemas.profiles import LlmProfile


def test_transport_retry_recovers_one_ssl_eof_with_request_local_budget() -> None:
    attempts = 0
    retries: list[tuple[int, int, str]] = []
    delays: list[float] = []

    def fake_call(_profile: LlmProfile, _request: ChatRequest) -> ChatResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(
                "OpenAI-compatible provider request failed: "
                "[SSL: UNEXPECTED_EOF_WHILE_READING]"
            )
        return _result()

    result = call_llm_with_transport_retries(
        _profile(),
        _request(),
        retry_limit=3,
        llm_call=fake_call,
        on_retry=lambda retry, limit, exc: retries.append(
            (retry, limit, str(exc))
        ),
        sleep_call=delays.append,
    )

    assert result.content == "ok"
    assert attempts == 2
    assert retries == [
        (
            1,
            3,
            "OpenAI-compatible provider request failed: "
            "[SSL: UNEXPECTED_EOF_WHILE_READING]",
        )
    ]
    assert delays == [0.5]


def test_transport_retry_recovers_http2_internal_stream_error() -> None:
    attempts = 0
    retries: list[tuple[int, int, str]] = []

    def fake_call(_profile: LlmProfile, _request: ChatRequest) -> ChatResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(
                "OpenAI-compatible provider stream failed: stream error: "
                "stream ID 1; INTERNAL_ERROR; received from peer"
            )
        return _result()

    result = call_llm_with_transport_retries(
        _profile(),
        _request(),
        retry_limit=3,
        llm_call=fake_call,
        on_retry=lambda retry, limit, exc: retries.append(
            (retry, limit, str(exc))
        ),
        sleep_call=lambda _delay: None,
    )

    assert result.content == "ok"
    assert attempts == 2
    assert retries == [
        (
            1,
            3,
            "OpenAI-compatible provider stream failed: stream error: "
            "stream ID 1; INTERNAL_ERROR; received from peer",
        )
    ]


def test_transport_retry_does_not_retry_non_transient_provider_rejection() -> None:
    attempts = 0

    def fake_call(_profile: LlmProfile, _request: ChatRequest) -> ChatResult:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("OpenAI-compatible provider returned 400: invalid request")

    try:
        call_llm_with_transport_retries(
            _profile(),
            _request(),
            retry_limit=3,
            llm_call=fake_call,
            sleep_call=lambda _delay: None,
        )
    except RuntimeError as exc:
        assert "returned 400" in str(exc)
    else:
        raise AssertionError("A deterministic provider rejection was accepted.")

    assert attempts == 1


def test_transport_retry_does_not_mask_auth_failure_behind_http_503() -> None:
    attempts = 0

    def fake_call(_profile: LlmProfile, _request: ChatRequest) -> ChatResult:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(
            'OpenAI-compatible provider returned 503: '
            '{"error":{"message":"auth_unavailable: no auth available"}}'
        )

    try:
        call_llm_with_transport_retries(
            _profile(),
            _request(),
            retry_limit=3,
            llm_call=fake_call,
            sleep_call=lambda _delay: None,
        )
    except RuntimeError as exc:
        assert "auth_unavailable" in str(exc)
    else:
        raise AssertionError("An authentication failure entered transport retries.")

    assert attempts == 1


def test_transport_budget_is_fresh_for_each_independent_request() -> None:
    attempts_by_request = {"first": 0, "second": 0}

    def run(request_id: str) -> ChatResult:
        def fake_call(_profile: LlmProfile, _request: ChatRequest) -> ChatResult:
            attempts_by_request[request_id] += 1
            if attempts_by_request[request_id] == 1:
                raise RuntimeError("temporary provider failure")
            return _result()

        return call_llm_with_transport_retries(
            _profile(),
            _request(),
            retry_limit=1,
            llm_call=fake_call,
            sleep_call=lambda _delay: None,
        )

    assert run("first").content == "ok"
    assert run("second").content == "ok"
    assert attempts_by_request == {"first": 2, "second": 2}


def test_transport_retry_prefers_provider_retry_after_with_a_local_cap() -> None:
    attempts = 0
    delays: list[float] = []

    def fake_call(_profile: LlmProfile, _request: ChatRequest) -> ChatResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderCallError(
                protocol="openai-compatible",
                kind="rate_limit",
                stage="request",
                detail="slow down",
                retryable=True,
                status_code=429,
                retry_after_seconds=120,
            )
        return _result()

    result = call_llm_with_transport_retries(
        _profile(),
        _request(),
        retry_limit=3,
        llm_call=fake_call,
        sleep_call=delays.append,
    )

    assert result.content == "ok"
    assert attempts == 2
    assert delays == [30.0]


def _profile() -> LlmProfile:
    return LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="model",
    )


def _request() -> ChatRequest:
    return ChatRequest(
        profile_id="main",
        stream=False,
        messages=[ChatMessage(role="user", content="Hello")],
    )


def _result() -> ChatResult:
    return ChatResult(
        content="ok",
        model_snapshot="model",
        provider_snapshot="openai-compatible",
    )
