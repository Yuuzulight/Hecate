# Multi-provider RAG (Gemini/Anthropic/OpenAI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the RAG answering chain and evaluation judge use a config-selected provider (`RAG_PROVIDER=gemini|anthropic|openai`, default `gemini`) instead of being hardcoded to Claude, so `/ask` and evaluations work without spending Anthropic credits the account doesn't have.

**Architecture:** A new `pipeline/rag/providers.py` module owns all provider-specific construction (client, model name, pricing, structured-output binding). `chain.py` and `evaluation.py` both build their models through it instead of constructing `ChatAnthropic`/`ragas.llm_factory` directly.

**Tech Stack:** LangChain (`langchain-anthropic`, `langchain-openai`, `langchain-google-genai`), RAGAS 0.4.3, FastAPI, pytest.

## Global Constraints

- `RAG_PROVIDER` defaults to `gemini`, validated against `{"gemini", "anthropic", "openai"}` at `Config()` construction — invalid values raise `ConfigError` immediately, not on first request.
- Selection is manual only. No automatic cross-provider fallback (deferred to a future issue — see spec's "Deferred" section).
- `effort` (Claude's thinking-effort setting) must not silently become shared: the judge never set it before this change and must keep relying on Claude's own default when `RAG_PROVIDER=anthropic`; only `chain.py` passes `effort="medium"` explicitly.
- `providers.build_structured_model` always binds with `include_raw=True` — this is not caller-configurable, because `_record_spend` depends on it and a missing raw message fails silently (answer still works, spend panel goes quiet).
- Every doc/comment claiming "the judge is Claude" or "you need an Anthropic key" must be updated in the same task that changes the code it describes — five locations found during spec review: `ARCHITECTURE.md` (two places), `README.md` (three places), `.env.example`, `k8s/10-rag-api.yaml`, and `evaluation.py`'s own module docstring.
- The two known integration risks (Gemini's `with_structured_output` shape, RAGAS's `LangchainLLMWrapper` compatibility with the `collections` metrics API) must be verified by a **manual run with real credentials** — CI never uses real provider keys (`.github/workflows/test.yml` only sets fake ones via `monkeypatch`), so a green CI run cannot substitute for this.

Spec: `docs/superpowers/specs/2026-08-12-rag-multi-provider-design.md`

---

## Task 1: Config gains `google_api_key` and `rag_provider`

**Files:**
- Modify: `pipeline/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.google_api_key: str` (optional, default `""`), `Config.rag_provider: str` (default `"gemini"`, one of `"gemini"`/`"anthropic"`/`"openai"`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`, after `test_repr_hides_the_password`:

```python
def test_google_api_key_defaults_to_blank(env):
    assert Config().google_api_key == ""


def test_rag_provider_defaults_to_gemini(env):
    assert Config().rag_provider == "gemini"


@pytest.mark.parametrize("value", ["anthropic", "openai", "gemini", "ANTHROPIC", "OpenAI"])
def test_rag_provider_accepts_the_known_values_case_insensitively(env, value):
    env.setenv("RAG_PROVIDER", value)
    assert Config().rag_provider == value.lower()


def test_an_unknown_rag_provider_is_an_error(env):
    env.setenv("RAG_PROVIDER", "chatgpt")
    with pytest.raises(ConfigError, match="RAG_PROVIDER"):
        Config()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -v -k "google_api_key or rag_provider"`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'google_api_key'` (and similarly for `rag_provider`).

- [ ] **Step 3: Implement in `pipeline/config.py`**

Add after the existing `self.anthropic_api_key = _optional("ANTHROPIC_API_KEY")` line (currently line 88) and before `self.rag_enabled = ...`:

```python
        # - Optional for the same reason as the other two keys: importing
        #   this module and running every test that doesn't touch the
        #   network must work without it.
        self.google_api_key = _optional("GOOGLE_API_KEY")

        # - Picks which of the three keys above actually gets used. Default
        #   is gemini, not anthropic: Gemini's free tier is what makes /ask
        #   and evaluation runs possible without Anthropic credits the
        #   account doesn't have. Validated here rather than left to fail on
        #   first use, matching how _int() already validates other settings.
        self.rag_provider = _optional("RAG_PROVIDER", "gemini").lower()
        if self.rag_provider not in ("gemini", "anthropic", "openai"):
            raise ConfigError(
                f"RAG_PROVIDER must be one of gemini, anthropic, openai - got {self.rag_provider!r}"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: All PASS, including the pre-existing tests (no regressions).

- [ ] **Step 5: Commit**

```bash
git add pipeline/config.py tests/test_config.py
git commit -m "Add GOOGLE_API_KEY and RAG_PROVIDER to Config"
```

---

## Task 2: `pipeline/rag/providers.py` — the shared provider module

**Files:**
- Create: `pipeline/rag/providers.py`
- Create: `tests/test_providers.py`
- Modify: `requirements-rag.txt`

**Interfaces:**
- Consumes: `Config.rag_provider`, `Config.google_api_key`, `Config.anthropic_api_key`, `Config.openai_api_key` (Task 1).
- Produces:
  - `ProviderSpec` (frozen dataclass): `.name: str`, `.model: str`, `.price_per_mtok_input: float`, `.price_per_mtok_output: float`
  - `spec_for(config: Config) -> ProviderSpec`
  - `build_chat_model(config: Config, max_tokens: int, effort: str | None = None) -> object`
  - `build_structured_model(config: Config, schema: type, max_tokens: int, effort: str | None = None) -> object`

- [ ] **Step 1: Add the two new dependencies**

Edit `requirements-rag.txt`, adding after the existing `langchain-anthropic==1.5.4` line:

```
langchain-google-genai==2.1.5
langchain-openai==0.3.3
```

Run: `pip install -r requirements-rag.txt` and confirm both import: `python -c "import langchain_google_genai, langchain_openai"` should print nothing and exit 0. If either version doesn't resolve against the pinned `langchain-core==1.5.3`, bump to whatever minor version does — the exact patch version matters less than confirming the import succeeds before moving on, the same lesson `requirements-eval.txt`'s comment already documents about `langchain-community`.

- [ ] **Step 1b: Confirm each integration's default retry behavior — the spec's rate-limits decision, not yet checked**

The spec decided to rely on each LangChain integration's own default retry/backoff for rate limits (Gemini's free tier has a low RPM cap) rather than write bespoke retry logic — but explicitly said to *confirm* the actual default retry count during implementation, not assume it. Check both:

```bash
python -c "
from langchain_google_genai import ChatGoogleGenerativeAI
import inspect
print(inspect.signature(ChatGoogleGenerativeAI.__init__).parameters.get('max_retries'))
"
python -c "
from langchain_anthropic import ChatAnthropic
import inspect
print(inspect.signature(ChatAnthropic.__init__).parameters.get('max_retries'))
"
```

Expected: both print a `Parameter` showing a nonzero default (LangChain's chat model base class typically defaults `max_retries` to 2). If either prints `None` (parameter doesn't exist) or a default of `0`, that provider's branch in `build_chat_model` (Step 4 below) needs an explicit `max_retries=2` (or similar) passed at construction — don't ship the assumption unverified. Note whichever is true for each provider in a one-line comment in `providers.py` next to that branch.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_providers.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.rag.providers'`.

- [ ] **Step 4: Look up GPT-5.1's real per-token pricing before writing the file**

The spec deliberately left this unguessed rather than fabricate a number — pull it from OpenAI's published pricing page (`https://openai.com/api/pricing/` or the current equivalent) the same way `chain.py`'s existing Claude prices are sourced from Anthropic's. Note the input and output price per million tokens; they go directly into the `SPECS` dict below, replacing the placeholder values shown in this step (do not commit the placeholder numbers — they're here only so the step is runnable if you paste it as-is before substituting).

- [ ] **Step 5: Write the implementation**

Create `pipeline/rag/providers.py`, substituting the real GPT-5.1 prices found in Step 4 for the `<input price>`/`<output price>` placeholders below:

```python
"""Which LLM answers and judges, chosen by one setting.

Three providers, one interface: a bare chat model any caller can wrap
(the judge does, via ragas.llms.LangchainLLMWrapper), and one already bound
to an answer schema (the answering chain uses this one). Gemini is the
default because its free tier is what makes any of this runnable without
Anthropic credits the account doesn't have - see the design spec for the
full reasoning, including why a unifying library like LiteLLM was set aside
in favour of three explicit branches.
"""

from dataclasses import dataclass

from pipeline.config import Config
from pipeline.exceptions import ConfigError


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    model: str
    price_per_mtok_input: float
    price_per_mtok_output: float


SPECS = {
    "gemini": ProviderSpec("gemini", "gemini-2.5-flash", 0.0, 0.0),
    "anthropic": ProviderSpec("anthropic", "claude-opus-5", 5.00, 25.00),
    # - Pulled from OpenAI's published pricing page (openai.com/api/pricing),
    #   the same way the Claude prices above are sourced from Anthropic's,
    #   rather than guessed here - see the plan's Step 4 for where these
    #   numbers came from.
    "openai": ProviderSpec("openai", "gpt-5.1", <input price>, <output price>),
}


def spec_for(config: Config) -> ProviderSpec:
    # config.rag_provider is already validated at Config construction time,
    # so this is a lookup, not a second validation.
    return SPECS[config.rag_provider]


def build_chat_model(config: Config, max_tokens: int, effort: str | None = None) -> object:
    """The bare chat model for the configured provider.

    Branches on spec.name because the three SDKs take genuinely different
    constructor arguments (max_output_tokens vs max_tokens, an effort field
    that only Claude has) - a generic signature here would just be a worse
    version of three specific ones. Raises ConfigError naming the missing
    key if the configured provider's key is not set. Used directly by the
    judge, which needs a bare model to wrap rather than one already bound to
    an answer schema.

    effort is passed through to Claude's output_config only when given, and
    ignored by the other two branches. Optional and unset by default because
    the judge - the only caller that does not set it - relied on Claude's
    own default (unset means "high") before this module existed, and a
    shared builder defaulting it to chain.py's "medium" would have changed
    the judge's behaviour as a side effect of an unrelated refactor.
    """
    spec = spec_for(config)

    if spec.name == "gemini":
        if not config.google_api_key:
            raise ConfigError("GOOGLE_API_KEY is required for RAG_PROVIDER=gemini")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=spec.model,
            google_api_key=config.google_api_key,
            max_output_tokens=max_tokens,
        )

    if spec.name == "anthropic":
        if not config.anthropic_api_key:
            raise ConfigError("ANTHROPIC_API_KEY is required for RAG_PROVIDER=anthropic")
        from langchain_anthropic import ChatAnthropic

        kwargs = {"model": spec.model, "max_tokens": max_tokens}
        if effort is not None:
            kwargs["output_config"] = {"effort": effort}
        return ChatAnthropic(**kwargs)

    if spec.name == "openai":
        if not config.openai_api_key:
            raise ConfigError("OPENAI_API_KEY is required for RAG_PROVIDER=openai")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=spec.model,
            api_key=config.openai_api_key,
            max_completion_tokens=max_tokens,
        )

    raise ConfigError(f"no builder for provider {spec.name!r}")  # pragma: no cover


# - "json_schema" dodges the bug documented in chain.py: the default method
#   forces tool_choice, which the API rejects whenever thinking is on, and
#   Claude Opus 5 has thinking on by default. OpenAI's structured output
#   supports the same method name. Gemini's integration takes no method
#   argument at all - confirmed by the manual smoke test in Task 2, not
#   assumed.
_STRUCTURED_OUTPUT_METHOD = {"gemini": None, "anthropic": "json_schema", "openai": "json_schema"}


def build_structured_model(
    config: Config, schema: type, max_tokens: int, effort: str | None = None
) -> object:
    """build_chat_model, already bound to a schema via with_structured_output.

    Which method argument each provider needs - if any - is exactly the kind
    of provider-specific knowledge that belongs in this module rather than
    leaked to callers as a field they have to branch on themselves. chain.py
    calls this and never needs to know that method="json_schema" is an
    Anthropic-and-OpenAI thing, or why.

    Always binds with include_raw=True, not exposed as a parameter: the only
    caller is chain.py, and it needs the raw message alongside the parsed
    object to read token usage off for _record_spend. Without it, an answer
    still comes back fine and the spend panel just goes quiet - the failure
    mode this project keeps a running list of.
    """
    spec = spec_for(config)
    chat = build_chat_model(config, max_tokens=max_tokens, effort=effort)

    kwargs = {"include_raw": True}
    method = _STRUCTURED_OUTPUT_METHOD[spec.name]
    if method is not None:
        kwargs["method"] = method
    return chat.with_structured_output(schema, **kwargs)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_providers.py -v`
Expected: All PASS. If `test_build_structured_model_binds_include_raw` or `test_build_structured_model_uses_json_schema_on_anthropic` fail because the `RunnableParallel` shape differs from what `chain.py`'s existing `bound_model` helper assumes, check the actual object structure with `python -c` before changing the test - LangChain's exact wrapping has changed across versions before (see `chain.py`'s `bound_model` docstring), so trust what's actually returned over what this plan assumed.

- [ ] **Step 7: Manual smoke test — real Gemini call (do this now, not later)**

This cannot be automated in CI (no real key there) and is the risk flagged first in the spec — verify it before Task 3 builds `chain.py` around this module. Run by hand with a real `GOOGLE_API_KEY`:

```python
# scratch.py - delete after running
from pipeline.config import Config
from pipeline.rag.providers import build_structured_model
from pydantic import BaseModel

class TestAnswer(BaseModel):
    text: str
    confidence: str

import os
os.environ["RAG_PROVIDER"] = "gemini"
# os.environ["GOOGLE_API_KEY"] must already be set to a real key
model = build_structured_model(Config(), TestAnswer, max_tokens=500)
result = model.invoke("Say hello and set confidence to 'high'.")
print("parsed:", result.get("parsed") if isinstance(result, dict) else result)
raw = result.get("raw") if isinstance(result, dict) else None
print("usage_metadata:", getattr(raw, "usage_metadata", "MISSING"))
```

Run: `DB_PASSWORD=x python scratch.py`

Expected: prints a parsed `TestAnswer` and a non-empty `usage_metadata` dict with `input_tokens`/`output_tokens`. If `usage_metadata` is missing or empty, `_record_spend` in Task 3 will silently log nothing for Gemini-answered questions — fix `build_chat_model`'s Gemini branch (likely needs an explicit flag to request usage metadata) before continuing. If the call raises on the `method=None` structured-output path, try `method="json_schema"` in `_STRUCTURED_OUTPUT_METHOD["gemini"]` and re-run.

Delete `scratch.py` when done.

- [ ] **Step 8: Commit**

```bash
git add pipeline/rag/providers.py tests/test_providers.py requirements-rag.txt
git commit -m "Add pipeline/rag/providers.py for provider-selectable LLM construction"
```

---

## Task 3: `chain.py` uses `providers.py`

**Files:**
- Modify: `pipeline/rag/chain.py`
- Test: `tests/test_rag_chain.py`

**Interfaces:**
- Consumes: `providers.build_structured_model`, `providers.spec_for` (Task 2).
- Produces: `AnswerChain(retriever, config, model=None)` (config now required), `AnswerChain.model_name: str`, `build_model(config) -> object`.

- [ ] **Step 1: Update `chain_returning` and the tests that call `build_model`/`build_chat_model` (write the failing state first)**

In `tests/test_rag_chain.py`, change the `chain_returning` helper (currently line 73-84):

```python
def chain_returning(**kwargs):
    answer = GroundedAnswer(
        answer=kwargs.pop("answer", "Python gained the most stars."),
        confidence=kwargs.pop("confidence", "high"),
        sources=Sources(
            repository_ids=kwargs.pop("repository_ids", ["github_1"]),
            blocks=kwargs.pop("blocks", ["language_growth"]),
        ),
    )
    retriever = StubRetriever(kwargs.pop("context", None))
    model = StubModel(answer)
    return AnswerChain(retriever, fake_config(), model=model), model
```

Add `fake_config` near the top of the file, after the `flat` helper:

```python
def fake_config(rag_provider="anthropic", anthropic_api_key="sk-ant-test"):
    """A Config-like object good enough for build_model - never actually
    reaches the network because the tests either stub the model or only
    inspect what build_model constructs without calling .invoke()."""
    from pipeline.config import Config

    import os

    old = {k: os.environ.get(k) for k in ("DB_PASSWORD", "RAG_PROVIDER", "ANTHROPIC_API_KEY")}
    os.environ["DB_PASSWORD"] = "secret"
    os.environ["RAG_PROVIDER"] = rag_provider
    if anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key
    try:
        return Config()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
```

Update the six tests that import from `pipeline.rag.chain` directly (currently lines 264, 270, 280, 290, 302, 327) — `test_the_model_does_not_force_a_tool_call`, `test_the_answer_schema_reaches_the_request`, `test_effort_survives_the_structured_output_binding`, `test_no_sampling_parameters_are_sent`, `test_the_raw_message_comes_back_so_spend_can_be_counted`, `test_tokens_and_cost_are_counted_from_the_raw_message`:

```python
def test_the_model_does_not_force_a_tool_call():
    from pipeline.rag.chain import build_model

    assert "tool_choice" not in bound_model(build_model(fake_config())).kwargs


def test_the_answer_schema_reaches_the_request():
    from pipeline.rag.chain import build_model

    bound = bound_model(build_model(fake_config())).kwargs
    schema = bound["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {"answer", "confidence", "sources"}


def test_effort_survives_the_structured_output_binding():
    from pipeline.rag.chain import EFFORT, build_model

    assert bound_model(build_model(fake_config())).kwargs["output_config"]["format"]
    from pipeline.rag import providers

    bare = providers.build_chat_model(fake_config(), max_tokens=100, effort=EFFORT)
    assert bare.output_config == {"effort": EFFORT}


def test_the_raw_message_comes_back_so_spend_can_be_counted():
    from pipeline.rag.chain import build_model

    built = build_model(fake_config())
    assert isinstance(built.first.steps__, dict)
    assert "raw" in built.first.steps__
```

`test_no_sampling_parameters_are_sent` moves to `tests/test_providers.py` in its entirety (it tests `providers.build_chat_model`'s Anthropic branch directly, nothing chain-specific) — delete it from `test_rag_chain.py` and add to `test_providers.py`:

```python
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
```

`test_tokens_and_cost_are_counted_from_the_raw_message` stays in `test_rag_chain.py` (it tests `chain._unwrap`, not provider construction) but its import changes:

```python
def test_tokens_and_cost_are_counted_from_the_raw_message():
    from pipeline import metrics
    from pipeline.rag import providers

    chain, _ = chain_returning()
    spec = providers.spec_for(chain.config)
    before_in = counter_value(metrics.rag_tokens, kind="input")
    before_cost = counter_value(metrics.rag_cost)

    answer = GroundedAnswer(answer="a", confidence="high", sources=Sources())
    chain._unwrap({
        "raw": FakeMessage({"input_tokens": 12000, "output_tokens": 400}),
        "parsed": answer,
        "parsing_error": None,
    })

    assert counter_value(metrics.rag_tokens, kind="input") - before_in == 12000
    expected = 12000 / 1e6 * spec.price_per_mtok_input + 400 / 1e6 * spec.price_per_mtok_output
    assert counter_value(metrics.rag_cost) - before_cost == pytest.approx(expected)
```

This requires `AnswerChain` to expose `self.config` — add that in Step 3 below.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rag_chain.py -v`
Expected: FAIL — `chain_returning` now calls `AnswerChain(retriever, fake_config(), model=model)`, a 3-positional-argument call the current `AnswerChain.__init__(self, retriever, model=None)` doesn't accept (`TypeError`).

- [ ] **Step 3: Rewrite `chain.py`**

Remove the module-level constants `MODEL`, `PRICE_PER_MTOK_INPUT`, `PRICE_PER_MTOK_OUTPUT` (currently lines 31, 57-58), and the `build_chat_model` function (currently lines 124-135) entirely. Keep `EFFORT` and `MAX_TOKENS` (lines 45, 50).

Add near the top, with the other imports:

```python
from pipeline.rag import providers
```

Replace `build_model` (currently lines 138-157):

```python
def build_model(config) -> object:
    """The model, already constrained to the answer schema.

    A thin wrapper around providers.build_structured_model, kept as its own
    function rather than inlined into AnswerChain.__init__ so the wiring can
    still be asserted directly - tests call this without needing to build a
    full AnswerChain first.
    """
    return providers.build_structured_model(config, GroundedAnswer, MAX_TOKENS, effort=EFFORT)
```

Replace `AnswerChain.__init__` and `answer_and_context` (currently lines 172-211):

```python
class AnswerChain:
    """Retrieve, ask, and hand back an answer that carries its evidence."""

    def __init__(self, retriever, config, model=None) -> None:
        self.retriever = retriever
        self.config = config
        self.log = get_logger("rag.chain")
        self.model = model if model is not None else build_model(config)
        self.model_name = providers.spec_for(config).model

    def answer(self, question: str) -> dict:
        return self.answer_and_context(question)[0]

    def answer_and_context(self, question: str) -> tuple[dict, dict]:
        """The answer, and the exact context it was built from.

        Evaluation needs both, and needs them to be the same retrieval. Asking
        the retriever again afterwards looks equivalent and is not: a snapshot
        landing in between would have the judge scoring the answer against
        evidence the answer never saw.
        """
        started = time.perf_counter()
        context = self.retriever.context_for(question)

        raw = self.model.invoke(
            [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"CONTEXT:\n{json.dumps(context, indent=2, default=str)}\n\n"
                        f"QUESTION: {question}"
                    )
                ),
            ]
        )
        result = self._unwrap(raw)

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._grounded(result, context, latency_ms), context
```

In `_record_spend` (currently lines 238-262), replace the cost calculation:

```python
    def _record_spend(self, message) -> None:
        """Tokens and approximate cost, per call.

        Never fatal: an answer that arrived is worth returning even if the
        accounting for it did not.
        """
        usage = getattr(message, "usage_metadata", None) or {}
        sent = usage.get("input_tokens") or 0
        received = usage.get("output_tokens") or 0
        if not (sent or received):
            return

        spec = providers.spec_for(self.config)
        cost = (sent / 1_000_000 * spec.price_per_mtok_input
                + received / 1_000_000 * spec.price_per_mtok_output)
        metrics.rag_tokens.labels(kind="input").inc(sent)
        metrics.rag_tokens.labels(kind="output").inc(received)
        metrics.rag_cost.inc(cost)
        self.log.info(
            "spend",
            extra={"context": {
                "input_tokens": sent,
                "output_tokens": received,
                "approx_cost_usd": round(cost, 6),
            }},
        )
```

In `_grounded` (currently lines 264-309), add `answer_model` to the returned dict:

```python
        return {
            "answer": result.answer,
            "confidence": result.confidence,
            "sources": {"repository_ids": cited, "blocks": blocks},
            "latency_ms": latency_ms,
            "answer_model": self.model_name,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rag_chain.py -v`
Expected: All PASS. `test_the_response_has_exactly_the_agreed_shape` (currently line 214-218) will now fail because it asserts `set(result) == {"answer", "confidence", "sources", "latency_ms"}` — add `"answer_model"` to that set:

```python
def test_the_response_has_exactly_the_agreed_shape():
    chain, _ = chain_returning()
    result = chain.answer("what is growing")
    assert set(result) == {"answer", "confidence", "sources", "latency_ms", "answer_model"}
    assert set(result["sources"]) == {"repository_ids", "blocks"}
```

Also `test_a_refusal_still_reports_what_was_looked_at`, `test_sources_survive_low_confidence`, and any other test asserting the full result dict shape needs the same addition if it enumerates keys exactly rather than checking a subset — grep the file for `set(result)` to find all of them.

- [ ] **Step 5: Run the full test file once more**

Run: `pytest tests/test_rag_chain.py tests/test_providers.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/rag/chain.py tests/test_rag_chain.py tests/test_providers.py
git commit -m "Make chain.py build its model through providers.py"
```

---

## Task 4: `evaluation.py` uses `providers.py`

**Files:**
- Modify: `pipeline/rag/evaluation.py`
- Test: `tests/test_rag_evaluation.py`

**Interfaces:**
- Consumes: `providers.build_chat_model`, `providers.spec_for` (Task 2).
- Produces: `build_judge(config) -> ragas.llms.LangchainLLMWrapper`, `Evaluator.evaluate(...)` row now reads `judge_model` from `providers.spec_for(self.config).model`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_rag_evaluation.py`, replace the `# ---- the judge is Claude` section (currently lines 222-253) with:

```python
# ---- the judge, wired to whichever provider is configured
#
# The real proof is the absence of an OpenAI request on a live run. These are
# the offline half: closing RAGAS's one side door back to OpenAI (an
# embeddings-taking relevance metric), which matters regardless of which
# provider judges.


def test_the_judge_is_wired_to_the_configured_provider(monkeypatch):
    monkeypatch.setenv("RAG_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    judge = build_judge(Config())
    # - LangchainLLMWrapper's exact attribute shape is unverified against
    #   ragas 0.4.3 as of writing (see the design spec's "Known risks") -
    #   this asserts the one thing that has to be true regardless of that
    #   shape: the wrapped model is the Anthropic one, not some other
    #   provider's, when RAG_PROVIDER says anthropic.
    from langchain_anthropic import ChatAnthropic

    assert isinstance(judge.langchain_llm, ChatAnthropic)


def test_the_judge_defaults_to_gemini_like_everything_else(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.delenv("RAG_PROVIDER", raising=False)
    judge = build_judge(Config())
    from langchain_google_genai import ChatGoogleGenerativeAI

    assert isinstance(judge.langchain_llm, ChatGoogleGenerativeAI)


def test_no_key_is_a_clear_error_rather_than_a_silent_fallback(monkeypatch):
    monkeypatch.setenv("RAG_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_judge(Config())


def test_neither_metric_takes_an_embeddings_model(monkeypatch):
    # - RAGAS' usual relevance metric embeds the answer to compare it, and
    #   Anthropic sells no embeddings API - so choosing it would have pulled
    #   OpenAI back in through the side door regardless of which provider the
    #   judge is set to. Both metrics here are LLM-only, and this is what
    #   keeps them that way, independent of provider.
    monkeypatch.setenv("RAG_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    for metric in build_metrics(build_judge(Config())).values():
        parameters = inspect.signature(type(metric).__init__).parameters
        assert "embeddings" not in parameters, f"{type(metric).__name__} would need embeddings"
```

**Note on `judge.langchain_llm`:** this attribute name is a best guess at what `ragas.llms.LangchainLLMWrapper` exposes, based on the RAGAS 0.4.3 pattern of wrapping a LangChain object and keeping a reference to it. Confirm the actual attribute name in Step 6's manual smoke test below (`python -c "from ragas.llms import LangchainLLMWrapper; help(LangchainLLMWrapper)"` or inspect a real instance) and fix these two tests to match before considering this task done — this is exactly the "verify the collections-metrics shape" risk the spec flagged, and this is where to resolve it.

Update `test_the_row_carries_both_models` (currently lines 215-219):

```python
def test_the_row_carries_both_models():
    ev = evaluator_with()
    row = ev.evaluate("what grew", ANSWER, CONTEXT)
    from pipeline.rag import providers

    assert row["judge_model"] == providers.spec_for(ev.config).model
    assert row["answer_model"] == "claude-opus-5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rag_evaluation.py -v -k "judge or row_carries"`
Expected: FAIL — `build_judge` still returns whatever the old `llm_factory` path returns, and `Evaluator` has no `.config` attribute yet.

- [ ] **Step 3: Rewrite the `evaluation.py` module docstring**

Replace the entire module docstring (currently lines 1-25):

```python
"""Scoring answers, and keeping the scores.

Every failure this project has had looked green at the time: vacuous dbt
tests, a CronJob collecting one source of four, an exporter frozen behind a
passing liveness probe, a CI watcher reporting success having parsed nothing.
A question-answering service is the easiest thing yet to make look healthy
while producing confident nonsense, because the output reads the same either
way. This is the check that can actually fail.

Two metrics, and the distinction between them is the point:

  faithfulness - is the answer supported by the context it was given?
                 Low means invented. That is a hallucination.
  relevance    - is the answer useful for the question asked?
                 Low means unhelpful. That is not a hallucination, and
                 reporting the two as one number loses the only distinction
                 worth having.

The judge is whichever provider RAG_PROVIDER names - built through
pipeline.rag.providers, the same module the answering chain uses. RAGAS
defaults to OpenAI and will quietly use it for anything it can, which would
mean a second key and a second bill; both metrics below were chosen partly
because they need only an LLM, regardless of which provider supplies it.

Reaching OpenAI through a second door was the real risk this used to guard
against with an Anthropic-only judge: RAGAS's usual relevance metric works by
embedding the answer to compare it, and neither Anthropic nor Gemini sells an
embeddings API in the same first-party way OpenAI's client offers here - so
choosing that metric would pull the OpenAI client back in no matter what the
judge is set to. Both metrics used here need only a language model, and a
test asserts that by inspecting their signatures rather than by reading the
setting.
"""
```

- [ ] **Step 4: Rewrite `build_judge` and remove `JUDGE_MODEL`**

Remove the `JUDGE_MODEL = "claude-opus-5"` constant (currently line 41) and the `import anthropic` line (currently line 31, no longer needed).

Add with the other imports:

```python
from pipeline.rag import providers
```

Replace `build_judge` (currently lines 122-131):

```python
def build_judge(config: Config):
    """The judge, on whichever provider is configured, with no OpenAI anywhere in the path."""
    from ragas.llms import LangchainLLMWrapper

    chat = providers.build_chat_model(config, max_tokens=JUDGE_MAX_TOKENS)
    return LangchainLLMWrapper(chat)
```

- [ ] **Step 5: Thread `config` through `Evaluator` and `evaluate()`**

`Evaluator.__init__` already stores `self.config = config` (existing code, unchanged) — confirm this is still true, it is. In `evaluate()` (currently lines 228-260), change the `row` dict construction:

```python
    def evaluate(self, question: str, answer: dict, context: dict, question_id=None) -> dict:
        """Score one answered question and return the row that would be stored."""
        scores = self.score(question, answer["answer"], context)
        faithfulness = scores.get("faithfulness")

        hallucination = None if faithfulness is None else faithfulness < HALLUCINATION_THRESHOLD

        row = {
            "question_id": question_id,
            "question": question,
            "answer": answer["answer"],
            "faithfulness": faithfulness,
            "relevance": scores.get("relevance"),
            "hallucination": hallucination,
            "judge_model": providers.spec_for(self.config).model,
            "answer_model": answer.get("answer_model", "unknown"),
            "latency_ms": answer.get("latency_ms"),
            "evaluated_at": datetime.now(timezone.utc),
        }
```

- [ ] **Step 6: Run tests, and confirm the `LangchainLLMWrapper` attribute name for real**

Run: `pytest tests/test_rag_evaluation.py -v`

If `test_the_judge_is_wired_to_the_configured_provider` fails on `judge.langchain_llm` with `AttributeError`, find the real attribute:

```bash
python -c "
from ragas.llms import LangchainLLMWrapper
from langchain_anthropic import ChatAnthropic
import inspect
w = LangchainLLMWrapper(ChatAnthropic(model='claude-opus-5', max_tokens=10, api_key='sk-ant-test'))
print([a for a in dir(w) if not a.startswith('_')])
"
```

Fix the attribute name in both tests to match, then re-run until green.

Expected: All PASS.

- [ ] **Step 7: Manual smoke test — real RAGAS `.score()` call against a `LangchainLLMWrapper`**

Do this before treating Task 4 as done — it's the second known risk from the spec, and CI cannot verify it (no real key). Run by hand with a real `GOOGLE_API_KEY` or `ANTHROPIC_API_KEY`:

```python
# scratch.py - delete after running
import os
os.environ["RAG_PROVIDER"] = "gemini"  # or "anthropic", whichever key you have
from pipeline.config import Config
from pipeline.rag.evaluation import build_judge, build_metrics

judge = build_judge(Config())
metrics = build_metrics(judge)
result = metrics["faithfulness"].score(
    user_input="What language grew the most?",
    response="Python grew the most, gaining 6081 stars.",
    retrieved_contexts=["language_growth: Python gained 6081 stars in the last day."],
)
print("faithfulness score:", result.value)
```

Run: `DB_PASSWORD=x python scratch.py`

Expected: prints a numeric score between 0 and 1. If it raises a `TypeError` or similar about the metric's expected LLM shape, `ragas.metrics.collections.Faithfulness` needs something `LangchainLLMWrapper` alone doesn't provide (per the spec's flagged risk) — check RAGAS 0.4.3's actual `Faithfulness.__init__` signature (`python -c "import inspect; from ragas.metrics.collections import Faithfulness; print(inspect.signature(Faithfulness.__init__))"`) and adjust `build_judge` accordingly, which may mean wrapping differently or pinning a different ragas version — this is exactly the kind of finding the spec said to expect and resolve here, not assume away.

Delete `scratch.py` when done.

- [ ] **Step 8: Commit**

```bash
git add pipeline/rag/evaluation.py tests/test_rag_evaluation.py
git commit -m "Make evaluation.py build its judge through providers.py"
```

---

## Task 5: Call sites pass `config` into `AnswerChain`

**Files:**
- Modify: `pipeline/rag/api.py:285`
- Modify: `pipeline/rag/evaluation.py` (the `main()` function, currently lines 282-341)

**Interfaces:**
- Consumes: `AnswerChain(retriever, config, model=None)` (Task 3).

- [ ] **Step 1: Update `api.py::main()`**

Change line 285 from:

```python
    app = build_app(config, chain=AnswerChain(retriever), retriever=retriever)
```

to:

```python
    app = build_app(config, chain=AnswerChain(retriever, config), retriever=retriever)
```

- [ ] **Step 2: Update `evaluation.py::main()`**

Remove the line `from pipeline.rag.chain import MODEL as ANSWER_MODEL` (currently line 284) — it's no longer needed since the chain sets `answer_model` itself now (Task 3).

Change:

```python
        chain = AnswerChain(retriever)
        rows = []
        for question in QUESTIONS:
            answer, context = chain.answer_and_context(question["question"])
            answer["answer_model"] = ANSWER_MODEL
            rows.append(
```

to:

```python
        chain = AnswerChain(retriever, config)
        rows = []
        for question in QUESTIONS:
            answer, context = chain.answer_and_context(question["question"])
            rows.append(
```

(The `answer["answer_model"] = ANSWER_MODEL` line is deleted — `answer_and_context` already returns a dict with `answer_model` set correctly per Task 3.)

- [ ] **Step 3: Run the existing tests that exercise `main()`**

Run: `pytest tests/test_rag_evaluation.py -v -k test_a_run_where_every_metric_failed_does_not_exit_clean`
Expected: PASS. This test monkeypatches `chain_module.AnswerChain` with a `StubChain` whose `__init__(self, retriever)` only takes one argument — check whether it needs updating to `__init__(self, retriever, config)` to match the new required-arg signature. If the test's `StubChain.__init__` doesn't accept the extra positional `config` argument `main()` now passes, update it:

```python
    class StubChain:
        def __init__(self, retriever, config):
            pass

        def answer_and_context(self, question):
            return {"answer": "something", "latency_ms": 1, "answer_model": "stub"}, CONTEXT
```

(Also added `"answer_model": "stub"` to the returned dict, matching the real chain's shape post-Task-3.)

- [ ] **Step 4: Run the full RAG test suite**

Run: `pytest tests/test_rag_chain.py tests/test_rag_evaluation.py tests/test_rag_api.py tests/test_providers.py tests/test_config.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/rag/api.py pipeline/rag/evaluation.py tests/test_rag_evaluation.py
git commit -m "Pass config into AnswerChain at both call sites"
```

---

## Task 6: Update the five stale-doc locations

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:** None — text only, no code.

- [ ] **Step 1: `ARCHITECTURE.md`**

Change line 160 from:

```
Phase 2 puts a question-answering service over the warehouse: retrieve context, ask Claude, return an answer with the rows it was built from. Most of the decisions in it are about what the model is *not* allowed to do.
```

to:

```
Phase 2 puts a question-answering service over the warehouse: retrieve context, ask whichever LLM `RAG_PROVIDER` names, return an answer with the rows it was built from. Most of the decisions in it are about what the model is *not* allowed to do.
```

Replace the `### Why the judge is Claude` section (currently lines 190-196) with:

```
### Why the judge, and the chain, are provider-selectable

Both used to be hardcoded to Claude. Both were blocked on the same thing at once: Anthropic account credits, separate from and unfunded by the Claude subscription. Gemini's free tier removes that blocker at this project's scale, so `RAG_PROVIDER` (`gemini` default / `anthropic` / `openai`) picks the provider for both the answering chain and the judge, built through one shared module, `pipeline/rag/providers.py`.

RAGAS defaults to OpenAI and will use it for anything it can. Left alone that means a second key, a second bill, and quality scores that cost more than the answers they grade - so the judge is always built from the same provider the chain answers with, never OpenAI regardless of what `RAG_PROVIDER` says.

Pointing it there is the easy half. The harder half is that RAGAS reaches OpenAI through a second door: its usual relevance metric works by embedding the answer to compare it, and neither Anthropic nor Gemini sells an embeddings API the same way - so choosing that metric would have pulled the OpenAI client back in regardless of which provider is configured. Both metrics used here need only a language model, and a test asserts that by inspecting their signatures rather than by reading the setting.

The two scores stay apart, because they mean different things. A low faithfulness score is an answer that invented something; a low relevance score is one that was merely unhelpful. Only the first is a hallucination, and reporting them as a single quality number loses the only distinction worth acting on. A metric that could not run at all is stored as null rather than zero - an unreachable judge is an outage, and a zero would be indistinguishable from a confident lie in every average taken afterwards.
```

- [ ] **Step 2: `README.md`**

Change the paragraph starting "**It needs an Anthropic API key.**" (currently line 244):

```
**It needs an API key for whichever provider is configured.** `RAG_PROVIDER` defaults to `gemini` — put `GOOGLE_API_KEY=...` in your `.env`, free via Google AI Studio. Set `RAG_PROVIDER=anthropic` or `RAG_PROVIDER=openai` and the matching key instead if you'd rather use one of those. Without a key for whichever provider is configured, the service still starts and `/trending` still works, but asking a question returns an error saying what's missing. Gemini's free tier costs nothing at this project's scale; Anthropic and OpenAI are billed per question — a few cents a day at any sane rate of asking, and there's an alert if it isn't.
```

Change the "Status" section's stale claim (currently line 268):

```
Running. All six sources collect, the models build, and the whole thing runs unattended on a schedule with alerting and nightly backups. The question-answering service on top of it is built and deployed, provider-selectable between Gemini, Anthropic, and OpenAI.
```

Change the "Stack" line (currently line 276):

```
Python 3.11, PostgreSQL 15, dbt, Docker, Kubernetes, Prometheus, Alertmanager, Grafana, Redis, FastAPI, LangChain, Gemini/Claude/GPT (provider-selectable).
```

- [ ] **Step 3: `.env.example`**

Replace the block from the `# API credentials` comment through `ANTHROPIC_API_KEY=` (currently lines 34-42):

```
# Only used by the embedding job and the similarity block. Unset, that job says
# so and exits non-zero, and questions are answered from the structured blocks
# alone - which is what carries the answers anyway. Unrelated to RAG_PROVIDER
# below - embeddings always use OpenAI regardless of which provider answers.
OPENAI_API_KEY=

# Which LLM answers questions and judges the answers, both drawn from the same
# provider via pipeline/rag/providers.py. Defaults to gemini specifically
# because its free tier needs nothing else set below to work.
RAG_PROVIDER=

# Free via Google AI Studio (console.cloud.google.com or aistudio.google.com) -
# what the default RAG_PROVIDER=gemini actually needs.
GOOGLE_API_KEY=

# Only needed if RAG_PROVIDER=anthropic. Separate balance from a Claude
# subscription - there is no supported path from one to the other.
ANTHROPIC_API_KEY=
```

(This also removes the old `OPENAI_API_KEY=` from further up the file if it was duplicated — check the file only has one `OPENAI_API_KEY=` line after this edit, since the original already had one at line 37 that this block's comment now references instead of duplicating.)

- [ ] **Step 4: Self-check for anything missed**

Run: `grep -rn "judge is Claude\|does not fall back to OpenAI\|needs an Anthropic\|billed per question" ARCHITECTURE.md README.md .env.example`
Expected: no output (the k8s manifest's instance is handled separately in Task 7).

- [ ] **Step 5: Commit**

```bash
git add ARCHITECTURE.md README.md .env.example
git commit -m "Update docs for provider-selectable RAG (Gemini default)"
```

---

## Task 7: Update and apply the k8s manifests

**Files:**
- Modify: `k8s/10-rag-api.yaml`

**Interfaces:** None — deployment config, verified against the live cluster.

- [ ] **Step 1: Add `google-api-key` to `rag-secret`, and update the deployment env**

Edit `k8s/10-rag-api.yaml`. Replace the comment and env block from `# - Only for the similarity block...` through the end of the `OPENAI_API_KEY` entry (currently lines 94-101):

```yaml
            # - Which provider answers and judges - see pipeline/rag/providers.py.
            #   Optional with no default in the manifest: unset, Config()
            #   defaults to gemini in the code, so the manifest doesn't need to
            #   repeat that default to get it.
            - name: RAG_PROVIDER
              value: "gemini"
            - name: GOOGLE_API_KEY
              valueFrom:
                secretKeyRef:
                  name: rag-secret
                  key: google-api-key
                  optional: true
            # - Only needed for embeddings (always OpenAI, regardless of
            #   RAG_PROVIDER) and for RAG_PROVIDER=openai.
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: rag-secret
                  key: openai-api-key
                  optional: true
```

(This keeps the existing `ANTHROPIC_API_KEY` block above it, currently lines 85-93, unchanged — only the comment and the entries below it change.)

- [ ] **Step 2: Add `google-api-key` to the live `rag-secret`**

This laptop's cluster is up right now (verified earlier this session: `hecate` namespace active, `rag-secret` exists with 2 keys). Add the third key without recreating the secret:

```bash
kubectl patch secret rag-secret -n hecate --type=json -p="[{\"op\": \"add\", \"path\": \"/data/google-api-key\", \"value\": \"$(echo -n 'YOUR_REAL_GOOGLE_API_KEY' | base64)\"}]"
```

Replace `YOUR_REAL_GOOGLE_API_KEY` with the real key (same one used in Task 2/4's manual smoke tests). Verify:

```bash
kubectl get secret rag-secret -n hecate -o jsonpath='{.data}' 
```

Expected: three keys now — `anthropic-api-key`, `google-api-key`, `openai-api-key`.

- [ ] **Step 3: Apply the updated manifest**

```bash
kubectl apply -f k8s/10-rag-api.yaml
kubectl rollout status deployment/hecate-rag -n hecate --timeout=60s
```

Expected: rollout completes, pod goes `1/1 Running`.

- [ ] **Step 4: Verify against the real pod, not just a rollout success message**

This project has a documented history of "committed but not applied" and "rolled out but not exercised" — don't stop at Step 3's green rollout.

```bash
kubectl exec -n hecate deploy/hecate-rag -- python -c "import os; print('RAG_PROVIDER=' + os.environ.get('RAG_PROVIDER', 'UNSET')); print('GOOGLE_API_KEY set:', bool(os.environ.get('GOOGLE_API_KEY')))"
```

Expected: `RAG_PROVIDER=gemini` and `GOOGLE_API_KEY set: True`.

Then a real end-to-end request through the actual running pod:

```bash
kubectl port-forward svc/hecate-rag 8001:8001 -n hecate &
sleep 2
curl -s -X POST http://localhost:8001/ask -H "Content-Type: application/json" -d '{"question":"What language grew the most?"}'
```

Expected: a JSON response with `"answer"`, `"confidence"`, `"sources"`, `"answer_model": "gemini-2.5-flash"` — not the `400 invalid_request_error` this session hit earlier when testing against Anthropic with no credits. Stop the port-forward when done (`kill %1` or `Ctrl+C`).

- [ ] **Step 5: Commit**

```bash
git add k8s/10-rag-api.yaml
git commit -m "Wire RAG_PROVIDER and GOOGLE_API_KEY into the rag deployment"
```

(The `kubectl patch secret` and `kubectl apply` steps above are cluster state, not committed to git — recorded here so the next person applying this manifest to a fresh cluster knows the secret needs the third key added by hand, same pattern as the existing two.)

---

## Task 8: Full suite verification

**Files:** None — verification only.

- [ ] **Step 1: Run the complete test suite**

Run: `pytest -q --cov=pipeline --cov-report=term-missing`
Expected: All tests pass, including every file touched in Tasks 1-5 and every pre-existing test untouched by this plan.

- [ ] **Step 2: Run the integration-tests-are-not-skipped check CI also runs**

Run: `HECATE_INTEGRATION=1 pytest -m integration -q`
Expected: tests run and pass (needs a real Postgres — the laptop's cluster has one at `127.0.0.1` via port-forward, or use `docker-compose up -d postgres` per `README.md`'s local setup).

- [ ] **Step 3: Confirm both manual smoke tests from Tasks 2 and 4 were actually run**

Not a pytest step — a checklist, because CI cannot verify this and it's the one thing in this plan a "tests pass" message doesn't cover:
- [ ] Task 2 Step 7 (real Gemini structured-output call) was run, and `usage_metadata` came back populated.
- [ ] Task 4 Step 7 (real RAGAS `.score()` call against a `LangchainLLMWrapper`) was run, and it returned a numeric score.
- [ ] Task 7 Step 4 (real `/ask` request against the deployed pod) returned a real answer, not an error.

If any of these three weren't actually done, this plan is not finished no matter what pytest says — this is exactly the failure shape (something reporting success without having been exercised) the spec called out explicitly.

- [ ] **Step 4: Push and confirm CI is green**

```bash
git push
```

Then check the GitHub Actions run for the `tests` workflow on both Python 3.11 and 3.14 — expected green, understanding per Step 3 above that CI green here means "the refactor didn't break anything CI can see with fake keys," not "the real API calls work." That's what Step 3's checklist is for.

- [ ] **Step 5: File the deferred automatic-fallback issue**

The spec's "Deferred" section explicitly set aside automatic runtime fallback (retry with a different provider if the configured one fails) in favor of the manual switch this plan built — captured there specifically so it isn't lost, per an earlier request in this project to save it as a future issue rather than build it now. File it once the manual version has actually shipped, referencing this plan's outcome:

```bash
gh issue create --repo Yuuzulight/Hecate \
  --title "Consider automatic provider fallback for RAG_PROVIDER" \
  --body "Manual provider selection (RAG_PROVIDER=gemini/anthropic/openai) shipped in $(git log -1 --format=%H). Automatic fallback - retry with the next provider in a list if the configured one fails at request time - was considered and deliberately set aside in favor of the manual switch (see docs/superpowers/specs/2026-08-12-rag-multi-provider-design.md, 'Deferred - not building now'). Worth revisiting once the manual version has been live long enough to know how often a switch would actually be needed. Open questions the original spec didn't answer: which failures are worth falling back on versus surfacing, and how answer_model/judge_model should record a fallback having happened rather than silently reporting the configured provider."
```

Expected: a new issue on the repo, low priority, no assignee — a placeholder so the idea isn't lost, not a commitment to build it.
