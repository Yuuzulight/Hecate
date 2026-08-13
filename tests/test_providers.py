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

PROVIDER_KEYS = {
    "gemini": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "secret")
    for name in ("RAG_PROVIDER", *PROVIDER_KEYS.values()):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---- spec_for and the provider-name declarations that back it


@pytest.mark.parametrize(
    "provider, model, price_in, price_out",
    [
        ("gemini", "gemini-3.5-flash", 0.0, 0.0),
        ("anthropic", "claude-opus-5", 5.00, 25.00),
        ("openai", "gpt-5.1", 3.00, 12.00),
    ],
)
def test_spec_for(env, provider, model, price_in, price_out):
    env.setenv("RAG_PROVIDER", provider)
    spec = providers.spec_for(Config())
    assert spec.name == provider
    assert spec.model == model
    assert spec.price_per_mtok_input == price_in
    assert spec.price_per_mtok_output == price_out


def test_provider_name_declarations_stay_in_sync():
    # - PROVIDER_NAMES is the single source of truth; SPECS,
    #   _STRUCTURED_OUTPUT_METHOD and _KEY_ATTR are each written by hand
    #   rather than derived from it (they carry per-provider data
    #   PROVIDER_NAMES doesn't), so nothing stops one of them drifting the
    #   day a fourth provider is added except a test that actually compares
    #   the key sets.
    assert set(providers.SPECS) == set(PROVIDER_NAMES)
    assert set(providers._STRUCTURED_OUTPUT_METHOD) == set(PROVIDER_NAMES)
    assert set(providers._KEY_ATTR) == set(PROVIDER_NAMES)


# ---- require_key: the check shared by build_chat_model and evaluation.py,
# and build_chat_model's own needs-a-key / constructs-with-a-key branches,
# which call it first thing - so a missing key fails the same way from
# either entry point, checked together rather than twice.


@pytest.mark.parametrize("provider, env_name", list(PROVIDER_KEYS.items()))
def test_a_missing_key_is_a_clear_error_from_either_entry_point(env, provider, env_name):
    env.setenv("RAG_PROVIDER", provider)
    config = Config()
    message = f"{env_name} is required for RAG_PROVIDER={provider}"

    with pytest.raises(ConfigError, match=message):
        providers.require_key(config, providers.spec_for(config))
    with pytest.raises(ConfigError, match=message):
        providers.build_chat_model(config, max_tokens=100)


@pytest.mark.parametrize("provider, env_name", list(PROVIDER_KEYS.items()))
def test_require_key_returns_the_configured_value(env, provider, env_name):
    env.setenv("RAG_PROVIDER", provider)
    env.setenv(env_name, "test-key")
    config = Config()
    assert providers.require_key(config, providers.spec_for(config)) == "test-key"


# ---- build_chat_model: the right client type per provider


@pytest.mark.parametrize(
    "provider, env_name, model_class_path",
    [
        ("gemini", "GOOGLE_API_KEY", "langchain_google_genai.ChatGoogleGenerativeAI"),
        ("anthropic", "ANTHROPIC_API_KEY", "langchain_anthropic.ChatAnthropic"),
        ("openai", "OPENAI_API_KEY", "langchain_openai.ChatOpenAI"),
    ],
)
def test_build_chat_model_constructs_with_a_key(env, provider, env_name, model_class_path):
    import importlib

    env.setenv("RAG_PROVIDER", provider)
    env.setenv(env_name, "test-key")
    module_path, class_name = model_class_path.rsplit(".", 1)
    model_class = getattr(importlib.import_module(module_path), class_name)

    model = providers.build_chat_model(Config(), max_tokens=100)
    assert isinstance(model, model_class)


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


# ---- effort: Claude-only, and never defaulted onto a caller that didn't ask


def test_effort_behavior(env):
    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    # - Given, it's applied.
    model = providers.build_chat_model(Config(), max_tokens=100, effort="medium")
    assert model.output_config == {"effort": "medium"}

    # - Not given, nothing is invented. This is the judge's path: it never
    #   passes effort, and the point is that build_chat_model does not
    #   invent one for it.
    model = providers.build_chat_model(Config(), max_tokens=100)
    assert model.output_config != {"effort": "medium"}

    # - Passed to a provider that has no such concept, it must not raise.
    env.setenv("RAG_PROVIDER", "gemini")
    env.setenv("GOOGLE_API_KEY", "test-key")
    providers.build_chat_model(Config(), max_tokens=100, effort="medium")


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


# ---- build_structured_model: everything the binding has to get right at
# once - include_raw, the method that dodges the tool_choice/thinking bug,
# the caller's actual schema reaching the request, and effort surviving
# alongside the schema bind. One construction, one raw message, four facts
# checked against it - they're all failure modes of the same bind call, not
# independent behaviors that happen to share setup.


def _steps(bound):
    """The RunnableParallel's steps dict, or None if there wasn't one.

    include_raw puts the model behind a RunnableParallel, so reaching the raw
    message is a step deeper than it was. Kept in one place so a LangChain
    version bump breaks one helper rather than every test that needs it.
    """
    return getattr(bound.first, "steps__", None) or getattr(bound.first, "steps", None)


def test_build_structured_model(env):
    from pydantic import BaseModel

    class Answer(BaseModel):
        headline: str
        certainty: str

    env.setenv("RAG_PROVIDER", "anthropic")
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    bound = providers.build_structured_model(Config(), Answer, max_tokens=100)
    steps = _steps(bound)

    # - The include_raw=True binding puts the model behind a RunnableParallel
    #   whose steps include "raw" - this is the thing that fails silently
    #   rather than with an error (see the spec's "Known risks" note).
    assert steps is not None and "raw" in steps
    raw = steps["raw"]

    # - json_schema is what dodges the tool_choice/thinking bug documented in
    #   chain.py - this proves the binding actually used it rather than
    #   falling back to the default method that would 400 on a live call.
    assert "tool_choice" not in raw.kwargs

    # - Whatever schema a caller passes in has to be the one that actually
    #   reaches the request, not just a schema - a binding that silently
    #   dropped or substituted fields would still "work" by every other
    #   check here and only fail once a real answer came back missing one.
    schema = raw.kwargs["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {"headline", "certainty"}

    # - effort lands on the model itself (set at construction, via
    #   build_chat_model), while the schema binding is a separate bind-time
    #   kwarg with_structured_output adds on top - two different places that
    #   both have to survive. If the bind replaced the model's own
    #   output_config field instead of layering a bind-time kwarg beside it,
    #   effort would vanish silently and only show up as a surprise on the
    #   bill. A second, effort-bearing build proves it, rather than assuming
    #   the first build's absence of effort generalizes.
    with_effort = providers.build_structured_model(Config(), Answer, max_tokens=100, effort="medium")
    raw_with_effort = _steps(with_effort)["raw"]
    assert raw_with_effort.bound.output_config == {"effort": "medium"}
    assert raw_with_effort.kwargs["output_config"]["format"]
