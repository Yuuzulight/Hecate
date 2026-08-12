"""Which LLM answers and judges, chosen by RAG_PROVIDER.

These test the branch logic and error messages directly - not by calling a
real API (that needs real credentials and is covered by a manual smoke test,
not CI; see the design spec's "Known risks" section).
"""

import pytest

from pipeline.config import Config
from pipeline.exceptions import ConfigError
from pipeline.rag import providers


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    for name in ("RAG_PROVIDER", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---- spec_for


def test_spec_for_gemini_is_the_default(env):
    spec = providers.spec_for(Config())
    assert spec.name == "gemini"
    assert spec.model == "gemini-2.5-flash"
    assert spec.price_per_mtok_input == 0.0
    assert spec.price_per_mtok_output == 0.0


def test_spec_for_anthropic(env):
    env.setenv("RAG_PROVIDER", "anthropic")
    spec = providers.spec_for(Config())
    assert spec.name == "anthropic"
    assert spec.model == "claude-opus-5"
    assert spec.price_per_mtok_input == 5.00
    assert spec.price_per_mtok_output == 25.00


def test_spec_for_openai(env):
    env.setenv("RAG_PROVIDER", "openai")
    spec = providers.spec_for(Config())
    assert spec.name == "openai"
    assert spec.model == "gpt-5.1"


# ---- build_chat_model: the right client type, or a clear error


def test_build_chat_model_gemini_needs_a_key(env):
    env.setenv("RAG_PROVIDER", "gemini")
    with pytest.raises(ConfigError, match="GOOGLE_API_KEY"):
        providers.build_chat_model(Config(), max_tokens=100)


def test_build_chat_model_gemini_constructs_with_a_key(env):
    env.setenv("RAG_PROVIDER", "gemini")
    env.setenv("GOOGLE_API_KEY", "test-key")
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = providers.build_chat_model(Config(), max_tokens=100)
    assert isinstance(model, ChatGoogleGenerativeAI)


def test_build_chat_model_anthropic_needs_a_key(env):
    env.setenv("RAG_PROVIDER", "anthropic")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        providers.build_chat_model(Config(), max_tokens=100)


def test_build_chat_model_anthropic_constructs_with_a_key(env):
    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from langchain_anthropic import ChatAnthropic

    model = providers.build_chat_model(Config(), max_tokens=100)
    assert isinstance(model, ChatAnthropic)


def test_build_chat_model_openai_needs_a_key(env):
    env.setenv("RAG_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        providers.build_chat_model(Config(), max_tokens=100)


def test_build_chat_model_openai_constructs_with_a_key(env):
    env.setenv("RAG_PROVIDER", "openai")
    env.setenv("OPENAI_API_KEY", "sk-test")
    from langchain_openai import ChatOpenAI

    model = providers.build_chat_model(Config(), max_tokens=100)
    assert isinstance(model, ChatOpenAI)


# ---- effort: Claude-only, and never defaulted onto a caller that didn't ask


def test_effort_is_applied_on_the_anthropic_branch(env):
    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    model = providers.build_chat_model(Config(), max_tokens=100, effort="medium")
    assert model.output_config == {"effort": "medium"}


def test_effort_left_unset_does_not_force_a_default(env):
    # - This is the judge's path: it never passes effort, and the point of
    #   this test is that build_chat_model does not invent one for it.
    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    model = providers.build_chat_model(Config(), max_tokens=100)
    assert model.output_config != {"effort": "medium"}


def test_effort_is_ignored_on_the_gemini_branch(env):
    # - Passing effort to a provider that has no such concept must not raise.
    env.setenv("RAG_PROVIDER", "gemini")
    env.setenv("GOOGLE_API_KEY", "test-key")
    providers.build_chat_model(Config(), max_tokens=100, effort="medium")  # must not raise


# ---- build_structured_model: schema binding, include_raw, provider method


def test_build_structured_model_binds_include_raw(env):
    from pydantic import BaseModel

    class Answer(BaseModel):
        text: str

    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    bound = providers.build_structured_model(Config(), Answer, max_tokens=100)
    # - The include_raw=True binding puts the model behind a RunnableParallel
    #   whose steps include "raw" - this is the one thing this test exists to
    #   catch, since it fails silently rather than with an error (see the
    #   spec's "Known risks" note on this exact failure mode).
    steps = getattr(bound.first, "steps__", None) or getattr(bound.first, "steps", None)
    assert "raw" in steps


def test_build_structured_model_uses_json_schema_on_anthropic(env):
    from pydantic import BaseModel

    class Answer(BaseModel):
        text: str

    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    bound = providers.build_structured_model(Config(), Answer, max_tokens=100)
    steps = getattr(bound.first, "steps__", None) or getattr(bound.first, "steps", None)
    raw = steps["raw"] if steps else bound.first
    # - json_schema is what dodges the tool_choice/thinking bug documented in
    #   chain.py - this proves the binding actually used it rather than
    #   falling back to the default method that would 400 on a live call.
    assert "tool_choice" not in raw.kwargs
