import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.config import AppConfig
from l_assistant.llm.factory import build_llm_client
from l_assistant.llm.groq_client import GroqClient
from l_assistant.llm.ollama_client import OllamaClient


def test_unknown_provider_rejected_by_config():
    with pytest.raises(ValueError):
        AppConfig(llm={"provider": "openai"})


def test_factory_builds_ollama_by_default():
    config = AppConfig()
    client = build_llm_client(config.llm)
    assert isinstance(client, OllamaClient)


def test_factory_builds_groq_when_selected():
    config = AppConfig(llm={"provider": "groq", "model": "openai/gpt-oss-20b"})
    client = build_llm_client(config.llm)
    assert isinstance(client, GroqClient)


def test_factory_respects_explicit_base_url_override():
    config = AppConfig(llm={"provider": "ollama", "base_url": "http://otherhost:11434"})
    client = build_llm_client(config.llm)
    assert isinstance(client, OllamaClient)
    # Internal attribute check is acceptable here: the point of this test
    # is only that an explicit base_url isn't silently discarded in favor
    # of the provider default.
    assert client._base_url == "http://otherhost:11434"


def test_groq_client_reads_api_key_from_configured_env_var(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_GROQ_KEY", "gsk_test123")
    config = AppConfig(
        llm={
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "api_key_env_var": "MY_CUSTOM_GROQ_KEY",
        }
    )
    client = build_llm_client(config.llm)
    assert client._api_key == "gsk_test123"


async def test_groq_ensure_ready_fails_fast_without_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from l_assistant.llm.base import LlmUnavailableError

    client = GroqClient(model="openai/gpt-oss-20b")
    with pytest.raises(LlmUnavailableError, match="GROQ_API_KEY"):
        await client.ensure_ready()
