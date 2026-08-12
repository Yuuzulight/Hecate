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

**The judge stops using `ragas.llms.llm_factory(provider=...)`.** RAGAS's factory only natively knows a handful of providers. `ragas.llms.LangchainLLMWrapper` wraps *any* LangChain chat model, so the judge is built from `providers.build_chat_model()` - the same per-provider construction logic the answering chain's `build_structured_model()` calls internally, just without the schema binding the judge doesn't need - instead of separate provider-specific judge wiring.

## `pipeline/rag/providers.py`

```python
@dataclass(frozen=True)
class ProviderSpec:
    name: str
    model: str
    price_per_mtok_input: float
    price_per_mtok_output: float   # 0.0 for gemini's free tier

SPECS = {
    "gemini":    ProviderSpec("gemini", "gemini-2.5-flash", 0.0, 0.0),
    "anthropic": ProviderSpec("anthropic", "claude-opus-5", 5.00, 25.00),
    "openai":    ProviderSpec("openai", "gpt-5.1", <real price>, <real price>),
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

def build_structured_model(config: Config, schema: type, max_tokens: int, effort: str | None = None) -> object:
    """build_chat_model, already bound to a schema via with_structured_output.

    Which `method` argument each provider needs - if any - is exactly the
    kind of provider-specific knowledge that belongs in this module rather
    than leaked to callers as a field they have to branch on themselves.
    chain.py calls this and never needs to know that method="json_schema"
    is an Anthropic-and-OpenAI thing, or why.

    Always binds with include_raw=True, not exposed as a parameter: the
    only caller is chain.py, and it needs the raw message alongside the
    parsed object to read token usage off for _record_spend. Without it,
    an answer still comes back fine and the spend panel just goes quiet -
    the failure mode this project keeps a running list of.
    """
```

GPT-5.1's per-token prices are left as a placeholder in this spec deliberately - pulled from OpenAI's published pricing page at implementation time, the same way the existing Claude prices in `chain.py` are sourced from Anthropic's, rather than guessed here.

**Known risks, not yet resolved - both need an early smoke test before the rest of the chain is built around them:**

- The Anthropic branch needs `method="json_schema"` specifically to dodge the `tool_choice`/thinking bug above. Whether Gemini's `with_structured_output` needs an equivalent workaround, or works with no `method` at all, is unknown - test one real Gemini call against the actual `GroundedAnswer` schema first, and confirm `usage_metadata` actually comes back populated on the raw message, not just that the answer parses. `langchain-google-genai` is the newest of the three integrations here; a schema that parses but a usage field that doesn't populate would pass every other check and just quietly zero out the spend panel for Gemini-answered questions.
- `ragas.metrics.collections.Faithfulness` and `RubricsScoreWithoutReference` (the API `evaluation.py` already uses) currently receive an LLM built by `ragas.llms.llm_factory(provider="anthropic", ...)`. Whether they accept a `ragas.llms.LangchainLLMWrapper`-wrapped model the same way, or expect something the collections API specifically requires, is unverified against ragas 0.4.3. Test one real `.score()` call with a wrapped model before rewiring the rest of `build_judge`.

## Changes to `chain.py`

- `AnswerChain.__init__(self, retriever, config, model=None)` - `config` becomes required. It was never threaded through before because `ChatAnthropic` read `ANTHROPIC_API_KEY` from the environment on its own; provider selection means the chain now has to know which provider it is building.
- `build_model(config)` stays as a named function in `chain.py` - a thin wrapper calling `providers.build_structured_model(config, GroundedAnswer, MAX_TOKENS, effort=EFFORT)` - rather than being inlined into `AnswerChain.__init__`, for the same reason the existing docstring already gives for splitting it out: so the wiring can still be asserted directly rather than only reachable by walking a built `AnswerChain`. `EFFORT = "medium"` stays a `chain.py` constant and is passed explicitly, same value as today. `build_chat_model` as a `chain.py`-local function is removed entirely - only `providers.build_chat_model` exists now.
- The chain carries `self.model_name = providers.spec_for(config).model` and includes it in its returned dict as `answer_model` - moves that field's source of truth into the chain itself, replacing `evaluation.py::main()`'s current `answer["answer_model"] = ANSWER_MODEL`, which reads a fixed import rather than what actually answered.
- `_record_spend()` reads `spec.price_per_mtok_input` / `price_per_mtok_output` instead of the current module-level `PRICE_PER_MTOK_INPUT` / `PRICE_PER_MTOK_OUTPUT` constants, so a Gemini-answered question correctly logs $0 rather than Claude's price.

## Changes to `evaluation.py`

- `build_judge(config)` becomes `ragas.llms.LangchainLLMWrapper(providers.build_chat_model(config, max_tokens=JUDGE_MAX_TOKENS))` - the bare model, not the structured one, since the judge scores free-text answers rather than emitting `GroundedAnswer` itself. `effort` is deliberately not passed here, preserving the judge's existing unset-effort behavior exactly.
- `JUDGE_MODEL` stops being a fixed module constant; `evaluate()` reads `providers.spec_for(config).model` fresh, so `rag_evaluations.judge_model` always reflects what actually judged that row rather than a name that could go stale if the provider changes between runs.

## Call sites

`api.py::main()` and `evaluation.py::main()` both already construct a `Config` before building the chain - each passes it into `AnswerChain(retriever, config)`, a one-line change at each site.

## Config (`pipeline/config.py`)

- `self.google_api_key = _optional("GOOGLE_API_KEY")`
- `self.rag_provider = _optional("RAG_PROVIDER", "gemini").lower()`, validated against `{"gemini", "anthropic", "openai"}` at construction, raising `ConfigError` on anything else - fail at startup, not on the first request, matching how `_int()` already validates other settings.

## Dependencies

Add `langchain-google-genai` and `langchain-openai` to `requirements-rag.txt`. Both are new: `embeddings.py` already talks to OpenAI, but through raw `requests` against the REST endpoint directly, not through LangChain - this is the first LangChain-OpenAI usage in the project. `requirements-eval.txt` inherits both via its existing `-r requirements-rag.txt`.

## Rate limits

Gemini's free tier caps requests per minute. `/ask` handles one question per call, so this is unlikely to matter there; `evaluation.py`'s run fires the fixed twelve-question set sequentially, which is naturally throttled by each call's own latency but could still land two calls close enough together to get a 429. Decision: rely on each LangChain integration's own default retry/backoff behavior (both `langchain-google-genai` and `langchain-anthropic` retry transient errors, including rate limits, by default) rather than add bespoke retry logic to `providers.py` - confirm each integration's actual default retry count during implementation rather than assume. This is a within-provider retry, distinct from the cross-provider fallback deferred below - a 429 that outlasts the built-in retries is still a hard failure, not a switch to a different provider.

## Documentation and deployment updates

- `ARCHITECTURE.md`'s existing `### Why the judge is Claude` section describes the judge as Claude specifically and explains why - both need rewriting once the judge is provider-selectable, or the doc actively contradicts the code the moment this ships.
- `k8s/` needs updating alongside the code, not after: `rag-secret` gains a `google-api-key` key (optional, same pattern as the existing `anthropic-api-key`/`openai-api-key`), and `10-rag-api.yaml` wires `GOOGLE_API_KEY` and `RAG_PROVIDER` into the deployment's env. This project has hit "committed but not applied to the cluster" before (the embed CronJob's manifest landing in a commit that didn't `kubectl apply`) - the implementation plan should apply and verify this against a real cluster, not just commit the YAML.
- `.env.example`'s comment on `ANTHROPIC_API_KEY` currently reads "Answering questions, and judging the answers. Both the chain and the RAGAS judge use this one key" - false the moment `RAG_PROVIDER` exists. Needs a `GOOGLE_API_KEY=` line (with a comment noting it's what the default provider actually needs) and a `RAG_PROVIDER=` line, and the existing comment rewritten so a fresh setup doesn't read instructions for a key its default provider doesn't use.
- `README.md` says outright "It needs an Anthropic API key" and describes answering as "billed per question - a few cents a day" - both wrong once the default provider is free. The "Stack" line listing "Claude" needs to say the answering model is provider-selectable rather than naming one.

## Selection behavior

Manual only - `RAG_PROVIDER` picks the provider for the whole deployment, and a failed call fails rather than silently retrying on a different provider. This matches `RAG_ENABLED`'s existing rollback-switch pattern (a manual, visible flag rather than something that decides on its own), and keeps `answer_model`/`judge_model` an honest record of what was actually configured rather than something a fallback could have silently substituted mid-run.

## Deferred - not building now

**Automatic runtime fallback** (try the configured provider, retry with the next one in a list on failure) was considered and set aside for this round in favor of the manual switch above. Worth its own issue once the manual version has been live long enough to know how often a switch would actually be needed - and it raises its own questions this spec doesn't answer: which failures are worth falling back on versus surfacing, and how `answer_model`/`judge_model` should record a fallback having happened rather than silently reporting the configured provider.

## Testing notes

Checked against the actual current test files rather than assumed - the impact is bigger than "add a config argument" in both files.

**`tests/test_rag_chain.py`:**
- `test_effort_survives_the_structured_output_binding`, `test_no_sampling_parameters_are_sent`, and `test_tokens_and_cost_are_counted_from_the_raw_message` import `build_chat_model`, `PRICE_PER_MTOK_INPUT`, and `PRICE_PER_MTOK_OUTPUT` directly from `pipeline.rag.chain`. All three are removed from that module's namespace entirely (moved to `providers.py`), so these imports `ImportError` at collection time, not just fail on a changed signature. These three tests move to a new `tests/test_providers.py` and test `providers.build_chat_model` / `ProviderSpec` directly, one instance per provider branch (each constructs the right client type, and raises `ConfigError` naming the right missing key when that provider's key is absent).
- `test_the_answer_schema_reaches_the_request` and the other `build_model()`-calling tests stay in `test_rag_chain.py`, updated only for the new required `config` argument.
- Tests constructing `AnswerChain(retriever, model=model)` with a stubbed model need the same `config` argument added - a fake `Config` with `rag_provider` set is enough, since the stub model bypasses `build_model()` entirely and never touches `providers.py`.

**`tests/test_rag_evaluation.py`:**
- The `# ---- the judge is Claude` section (`test_the_judge_is_wired_to_anthropic`, `test_no_key_is_a_clear_error_rather_than_a_silent_openai_fallback`, `test_neither_metric_takes_an_embeddings_model`) asserts Anthropic-specific internals - `judge.provider == "anthropic"`, `type(judge.client.client).__module__.startswith("anthropic")` - against whatever `llm_factory` used to return. Once `build_judge` returns a `LangchainLLMWrapper`, that shape almost certainly doesn't exist the same way, and `Config()` with no override now defaults to `gemini`, not `anthropic`, so the test wouldn't even be exercising what its name says without an explicit `RAG_PROVIDER=anthropic` override. The assertions need rewriting against whatever `LangchainLLMWrapper` actually exposes - verify that shape as part of the same early smoke test already called out as a known risk above, before rewriting these.
- `test_neither_metric_takes_an_embeddings_model`'s actual invariant - neither RAGAS metric's constructor takes an `embeddings` parameter, closing the side door back to OpenAI - stays correct and provider-agnostic. Only the Anthropic-specific setup around it needs to go; the assertion itself does not need to change.
- `test_the_row_carries_both_models` asserts `row["judge_model"] == evaluation.JUDGE_MODEL`, which breaks outright once that constant is removed. Rewrite against `providers.spec_for(config).model`.
