# Multi-provider RAG: Gemini, Anthropic, OpenAI

## Why

The answering chain (`chain.py`) and the evaluation judge (`evaluation.py`) are both hardcoded to Claude. Both are blocked on the same thing right now: the Anthropic account has no API credits, and the Claude Max subscription does not fund the API - they are separate balances, and there is no supported path from one to the other. `/ask` has never returned a real answer; `rag_evaluations` has never gained a row.

Google's Gemini API has a free tier (via Google AI Studio, separate from Vertex AI billing) that sidesteps the credit problem entirely for a project at this scale - a handful of questions a day and one evaluation run of twelve questions. This spec makes the provider a config choice instead of a hardcoded one, with Gemini as the default specifically because it removes the blocker without spending anything.

## Scope

Both files. The Anthropic credit blocker hits `chain.py`'s answering model and `evaluation.py`'s judge identically - fixing only the chain would leave evaluations stuck at zero rows for the same reason they are stuck today.

## Approach

A shared `pipeline/rag/providers.py` module, selected by one new setting, `RAG_PROVIDER` (`gemini` default / `anthropic` / `openai`). Both `chain.py` and `evaluation.py` build their model through it, so there is exactly one place that knows how to construct each provider's client.

Two approaches considered and set aside:

- **Branching separately in each file.** Duplicates the provider list in two places, and the two files would drift on model names or pricing the first time either one changes without the other.
- **A unifying library like LiteLLM.** The Anthropic wiring in `chain.py` already required tracking down a real bug - `with_structured_output`'s default method forces `tool_choice`, which the API rejects once thinking is on, and Claude Opus 5 has thinking on by default. Routing three providers through a new abstraction risks rediscovering that class of bug per-provider with less visibility into what is actually sent. Not worth it for three known providers with existing first-party LangChain integrations.

**The judge stops using `ragas.llms.llm_factory(provider=...)`.** RAGAS's factory only natively knows a handful of providers. `ragas.llms.LangchainLLMWrapper` wraps *any* LangChain chat model, so the judge uses the exact same `providers.build_chat_model()` the answering chain does, instead of separate provider-specific judge wiring.

## `pipeline/rag/providers.py`

```python
@dataclass(frozen=True)
class ProviderSpec:
    name: str
    model: str
    price_per_mtok_input: float
    price_per_mtok_output: float   # 0.0 for gemini's free tier
    structured_output_method: str | None   # "json_schema", or None to let the
                                            # integration pick its own default

SPECS = {
    "gemini":    ProviderSpec("gemini", "gemini-2.5-flash", 0.0, 0.0, None),
    "anthropic": ProviderSpec("anthropic", "claude-opus-5", 5.00, 25.00, "json_schema"),
    "openai":    ProviderSpec("openai", "gpt-5.1", <real price>, <real price>, "json_schema"),
}

def spec_for(config: Config) -> ProviderSpec:
    # config.rag_provider is already validated at Config construction time,
    # so this is a lookup, not a second validation.
    return SPECS[config.rag_provider]

def build_chat_model(config: Config, max_tokens: int) -> object:
    """The bare chat model for the configured provider.

    Branches on spec.name because the three SDKs take genuinely different
    constructor arguments (max_output_tokens vs max_tokens, an effort field
    that only Claude has) - a generic signature here would just be a worse
    version of three specific ones. Raises ConfigError naming the missing
    key if the configured provider's key is not set.
    """
```

GPT-5.1's per-token prices are left as a placeholder in this spec deliberately - pulled from OpenAI's published pricing page at implementation time, the same way the existing Claude prices in `chain.py` are sourced from Anthropic's, rather than guessed here.

**Known risk, not yet resolved:** the Anthropic branch needs `method="json_schema"` specifically to dodge the `tool_choice`/thinking bug above. Whether Gemini's `with_structured_output` has an equivalent landmine is unknown. The implementation plan should smoke-test one real Gemini call against the actual `GroundedAnswer` schema early, before the rest of the chain is built around it.

## Changes to `chain.py`

- `AnswerChain.__init__(self, retriever, config, model=None)` - `config` becomes required. It was never threaded through before because `ChatAnthropic` read `ANTHROPIC_API_KEY` from the environment on its own; provider selection means the chain now has to know which provider it is building.
- `build_model(config)` calls `providers.build_chat_model(config, max_tokens=MAX_TOKENS)`, then `.with_structured_output(GroundedAnswer, include_raw=True, **({"method": spec.structured_output_method} if spec.structured_output_method else {}))`.
- The chain carries `self.model_name = providers.spec_for(config).model` and includes it in its returned dict as `answer_model` - moves that field's source of truth into the chain itself, replacing `evaluation.py::main()`'s current `answer["answer_model"] = ANSWER_MODEL`, which reads a fixed import rather than what actually answered.
- `_record_spend()` reads `spec.price_per_mtok_input` / `price_per_mtok_output` instead of the current module-level `PRICE_PER_MTOK_INPUT` / `PRICE_PER_MTOK_OUTPUT` constants, so a Gemini-answered question correctly logs $0 rather than Claude's price.

## Changes to `evaluation.py`

- `build_judge(config)` becomes `ragas.llms.LangchainLLMWrapper(providers.build_chat_model(config, max_tokens=JUDGE_MAX_TOKENS))`.
- `JUDGE_MODEL` stops being a fixed module constant; `evaluate()` reads `providers.spec_for(config).model` fresh, so `rag_evaluations.judge_model` always reflects what actually judged that row rather than a name that could go stale if the provider changes between runs.

## Call sites

`api.py::main()` and `evaluation.py::main()` both already construct a `Config` before building the chain - each passes it into `AnswerChain(retriever, config)`, a one-line change at each site.

## Config (`pipeline/config.py`)

- `self.google_api_key = _optional("GOOGLE_API_KEY")`
- `self.rag_provider = _optional("RAG_PROVIDER", "gemini").lower()`, validated against `{"gemini", "anthropic", "openai"}` at construction, raising `ConfigError` on anything else - fail at startup, not on the first request, matching how `_int()` already validates other settings.

## Dependencies

Add `langchain-google-genai` and `langchain-openai` to `requirements-rag.txt`. Both are new: `embeddings.py` already talks to OpenAI, but through raw `requests` against the REST endpoint directly, not through LangChain - this is the first LangChain-OpenAI usage in the project. `requirements-eval.txt` inherits both via its existing `-r requirements-rag.txt`.

## Selection behavior

Manual only - `RAG_PROVIDER` picks the provider for the whole deployment, and a failed call fails rather than silently retrying on a different provider. This matches `RAG_ENABLED`'s existing rollback-switch pattern (a manual, visible flag rather than something that decides on its own), and keeps `answer_model`/`judge_model` an honest record of what was actually configured rather than something a fallback could have silently substituted mid-run.

## Deferred - not building now

**Automatic runtime fallback** (try the configured provider, retry with the next one in a list on failure) was considered and set aside for this round in favor of the manual switch above. Worth its own issue once the manual version has been live long enough to know how often a switch would actually be needed - and it raises its own questions this spec doesn't answer: which failures are worth falling back on versus surfacing, and how `answer_model`/`judge_model` should record a fallback having happened rather than silently reporting the configured provider.

## Testing notes

- `build_chat_model` should be tested per-branch: each provider constructs the right client type and raises `ConfigError` naming the right missing key when its key is absent.
- Existing tests that construct `AnswerChain(retriever)` with a stubbed model need updating for the new required `config` argument - a fake `Config` with `rag_provider` set is enough, since the stub model bypasses `build_model()` entirely.
- The `test that asserts by constructor signature that neither RAGAS metric takes an embeddings parameter` (existing, per `claude-api-integration-traps`) stays relevant regardless of judge provider and does not need to change.
