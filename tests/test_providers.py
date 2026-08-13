"""Which LLM answers and judges, chosen by RAG_PROVIDER.

These test the branch logic and error messages directly - not by calling a
real API (that needs real credentials and is covered by a manual smoke test,
not CI; see the design spec's "Known risks" section).
"""

import pytest

from pipeline.config import Config
from pipeline.exceptions import ConfigError
from pipeline.rag import providers
from pipeline.rag.provider_names import PROVIDER_NAMES


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
    assert spec.model == "gemini-3.5-flash"
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


# ---- the provider name set stays in sync across every place it's declared
#
# PROVIDER_NAMES is the single source of truth; SPECS, _STRUCTURED_OUTPUT_METHOD
# and _KEY_ATTR are each written by hand rather than derived from it (they
# carry per-provider data PROVIDER_NAMES doesn't), so nothing stops one of
# them drifting the day a fourth provider is added except a test that
# actually compares the key sets.


def test_specs_keys_match_provider_names():
    assert set(providers.SPECS) == set(PROVIDER_NAMES)


def test_structured_output_method_keys_match_provider_names():
    assert set(providers._STRUCTURED_OUTPUT_METHOD) == set(PROVIDER_NAMES)


def test_key_attr_keys_match_provider_names():
    assert set(providers._KEY_ATTR) == set(PROVIDER_NAMES)


# ---- require_key: the check shared by build_chat_model and evaluation.py


def test_require_key_returns_the_configured_value(env):
    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config = Config()
    assert providers.require_key(config, providers.spec_for(config)) == "sk-ant-test"


@pytest.mark.parametrize(
    "provider, env_name",
    [("gemini", "GOOGLE_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")],
)
def test_require_key_raises_naming_the_missing_one(env, provider, env_name):
    env.setenv("RAG_PROVIDER", provider)
    config = Config()
    with pytest.raises(ConfigError, match=f"{env_name} is required for RAG_PROVIDER={provider}"):
        providers.require_key(config, providers.spec_for(config))


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


def test_build_chat_model_anthropic_uses_configs_key_not_the_raw_env_var(env):
    # - ChatAnthropic falls back to its own (unstripped) os.getenv lookup if
    #   api_key isn't passed explicitly. Config's value is what was actually
    #   validated as present, so it has to be what the client authenticates
    #   with - proven here by making the two differ and checking which one
    #   wins, not just that construction succeeds.
    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-config-value")
    config = Config()
    assert config.anthropic_api_key == "sk-ant-config-value"

    env.setenv("ANTHROPIC_API_KEY", "sk-ant-different-runtime-value")
    model = providers.build_chat_model(config, max_tokens=100)
    assert model.anthropic_api_key.get_secret_value() == "sk-ant-config-value"


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


def test_no_sampling_parameters_are_sent_on_the_anthropic_branch(env):
    # - temperature, top_p and top_k are removed on this model and any of
    #   them is a 400. The scope asked for temperature 0.2; the prompt
    #   carries that intent instead (see chain.py's SYSTEM_PROMPT).
    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    model = providers.build_chat_model(Config(), max_tokens=100)
    assert model.temperature is None
    assert model.top_p is None
    assert model.top_k is None


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


def test_build_structured_model_schema_properties_reach_the_request(env):
    from pydantic import BaseModel

    class Answer(BaseModel):
        headline: str
        certainty: str

    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    bound = providers.build_structured_model(Config(), Answer, max_tokens=100)
    steps = getattr(bound.first, "steps__", None) or getattr(bound.first, "steps", None)
    raw = steps["raw"] if steps else bound.first
    # - Whatever schema a caller passes in has to be the one that actually
    #   reaches the request, not just a schema - a binding that silently
    #   dropped or substituted fields would still "work" by every other check
    #   here and only fail once a real answer came back missing a property.
    schema = raw.kwargs["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {"headline", "certainty"}


def test_build_structured_model_preserves_effort(env):
    from pydantic import BaseModel

    class Answer(BaseModel):
        text: str

    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    bound = providers.build_structured_model(Config(), Answer, max_tokens=100, effort="medium")
    steps = getattr(bound.first, "steps__", None) or getattr(bound.first, "steps", None)
    raw = steps["raw"] if steps else bound.first
    # - effort lands on the model itself (set at construction, via
    #   build_chat_model), while the schema binding is a separate bind-time
    #   kwarg with_structured_output adds on top - two different places that
    #   both have to survive. If the bind replaced the model's own
    #   output_config field instead of layering a bind-time kwarg beside it,
    #   effort would vanish silently here and only show up as a surprise on
    #   the bill.
    assert raw.bound.output_config == {"effort": "medium"}
    assert raw.kwargs["output_config"]["format"]
