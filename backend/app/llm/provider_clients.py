from collections import OrderedDict
from hashlib import sha256
from threading import RLock

from anthropic import Anthropic
from openai import OpenAI

from app.schemas.profiles import LlmProfile


_MAX_CACHED_CLIENTS_PER_PROTOCOL = 16
_CLIENT_LOCK = RLock()
_OPENAI_CLIENTS: OrderedDict[tuple[str, str], OpenAI] = OrderedDict()
_ANTHROPIC_CLIENTS: OrderedDict[tuple[str, str], Anthropic] = OrderedDict()


def get_openai_client(profile: LlmProfile) -> OpenAI:
    base_url, key_digest, api_key = _client_identity(profile)
    cache_key = (base_url, key_digest)
    with _CLIENT_LOCK:
        existing = _OPENAI_CLIENTS.get(cache_key)
        if existing is not None:
            _OPENAI_CLIENTS.move_to_end(cache_key)
            return existing
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )
        _OPENAI_CLIENTS[cache_key] = client
        _evict_openai_clients()
        return client


def get_anthropic_client(profile: LlmProfile) -> Anthropic:
    base_url, key_digest, api_key = _client_identity(profile)
    cache_key = (base_url, key_digest)
    with _CLIENT_LOCK:
        existing = _ANTHROPIC_CLIENTS.get(cache_key)
        if existing is not None:
            _ANTHROPIC_CLIENTS.move_to_end(cache_key)
            return existing
        client = Anthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
        )
        _ANTHROPIC_CLIENTS[cache_key] = client
        _evict_anthropic_clients()
        return client


def close_provider_clients() -> None:
    with _CLIENT_LOCK:
        clients: list[OpenAI | Anthropic] = [
            *_OPENAI_CLIENTS.values(),
            *_ANTHROPIC_CLIENTS.values(),
        ]
        _OPENAI_CLIENTS.clear()
        _ANTHROPIC_CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            # Shutdown cleanup is best effort; an already-broken transport must not
            # conceal the Harness shutdown state.
            pass


def _client_identity(profile: LlmProfile) -> tuple[str, str, str]:
    api_key = profile.api_key.get_secret_value()
    base_url = str(profile.base_url).rstrip("/") + "/"
    key_digest = sha256(api_key.encode("utf-8")).hexdigest()
    return base_url, key_digest, api_key


def _evict_openai_clients() -> None:
    while len(_OPENAI_CLIENTS) > _MAX_CACHED_CLIENTS_PER_PROTOCOL:
        _key, client = _OPENAI_CLIENTS.popitem(last=False)
        client.close()


def _evict_anthropic_clients() -> None:
    while len(_ANTHROPIC_CLIENTS) > _MAX_CACHED_CLIENTS_PER_PROTOCOL:
        _key, client = _ANTHROPIC_CLIENTS.popitem(last=False)
        client.close()
