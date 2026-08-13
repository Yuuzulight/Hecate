"""Which LLM answers and judges, chosen by one setting.

Three providers, one interface: a bare chat model any caller can wrap, and
one already bound to an answer schema built on top of it (the answering
chain uses this one, via build_structured_model). The judge in
evaluation.py does not call into this module for its model the way the
chain does - ragas 0.4.3's collections metrics need a raw, async-patched
provider SDK client rather than the LangChain chat model this module
returns, so evaluation.py builds that client itself (see its module
docstring for why, confirmed live rather than assumed). It still uses this
module's spec_for for the model name and pricing, the same lookup the chain
uses. Gemini is the default because its free tier is what makes any of this
runnable without Anthropic credits the account doesn't have - see the
design spec for the full reasoning, including why a unifying library like
LiteLLM was set aside in favour of three explicit branches. That removes the
Anthropic-credits blocker specifically, not every possible one: a Google
Cloud project still carries its own billing state, and live verification
confirmed the deployment/provider-selection wiring end-to-end on
RAG_PROVIDER=gemini while the answer itself was blocked by that project's own
prepay credits being exhausted (429 RESOURCE_EXHAUSTED) - an account-level
quota fact, not a code defect, but not something the free tier guarantees
away either.
"""

from dataclasses import dataclass

from pipeline.config import Config
from pipeline.exceptions import ConfigError
from pipeline.rag.provider_names import PROVIDER_NAMES


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    model: str
    price_per_mtok_input: float
    price_per_mtok_output: float


SPECS = {
    # - Switched from gemini-2.5-flash: it 404s for this account as of this
    #   session ("no longer available to new users" - retired), confirmed
    #   against a real key. gemini-3.5-flash is confirmed available and
    #   working against the same key.
    # - $0.00/$0.00 means hecate_rag_cost_usd_total never increments while
    #   running on the default provider, so RagSpendHigh (severity: critical,
    #   see k8s/monitoring/alert-rules.yaml) cannot fire regardless of usage
    #   under RAG_PROVIDER=gemini. RagTokensHigh, in that same file, is the
    #   replacement alert keyed on hecate_rag_tokens_total instead.
    "gemini": ProviderSpec("gemini", "gemini-3.5-flash", 0.0, 0.0),
    "anthropic": ProviderSpec("anthropic", "claude-opus-5", 5.00, 25.00),
    # - PLACEHOLDER pricing, not verified against OpenAI's real pricing page
    #   (no live browsing available when this was written) - confirm before
    #   this becomes financially load-bearing.
    "openai": ProviderSpec("openai", "gpt-5.1", 3.00, 12.00),
}


def spec_for(config: Config) -> ProviderSpec:
    # config.rag_provider is already validated at Config construction time,
    # so this is a lookup, not a second validation.
    return SPECS[config.rag_provider]


# - Which Config attribute and env var name each provider's key lives under.
#   The single thing require_key needs to do its job for all three, so the
#   check-and-raise itself exists exactly once rather than once per branch
#   per caller (this module's build_chat_model and evaluation.py's
#   _instructor_client both need it, and used to each spell it out inline,
#   with the same message duplicated in both places).
_KEY_ATTR = {
    "gemini": ("google_api_key", "GOOGLE_API_KEY"),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "openai": ("openai_api_key", "OPENAI_API_KEY"),
}


def require_key(config: Config, spec: ProviderSpec) -> str:
    """The configured provider's API key, or a ConfigError naming what's missing.

    Client *construction* still differs genuinely per provider - different
    SDKs, different constructor shapes - and stays three branches for that
    reason. This is the one piece of each branch that was pure duplication:
    the same "is it set, if not raise naming it" check, byte-identical
    message and all, existing twice per provider across two files.
    """
    attr, env_name = _KEY_ATTR[spec.name]
    value = getattr(config, attr)
    if not value:
        raise ConfigError(f"{env_name} is required for RAG_PROVIDER={spec.name}")
    return value


def build_chat_model(config: Config, max_tokens: int, effort: str | None = None) -> object:
    """The bare chat model for the configured provider.

    Branches on spec.name because the three SDKs take genuinely different
    constructor arguments (max_output_tokens vs max_tokens, an effort field
    that only Claude has) - a generic signature here would just be a worse
    version of three specific ones. Raises ConfigError naming the missing
    key if the configured provider's key is not set. build_structured_model,
    immediately below, is the only production caller - it wraps this in
    with_structured_output to bind an answer schema, which is what the
    answering chain actually uses. (evaluation.py's judge does not call this:
    it needs a raw provider SDK client, not a LangChain chat model - see this
    module's and evaluation.py's docstrings.)

    effort is passed through to Claude's output_config only when given, and
    ignored by the other two branches. Optional and unset by default: nothing
    in this module invents an effort for a caller that does not ask for one.
    That mattered originally because evaluation.py's judge - before this
    module existed, and before it moved to building its own client - called
    Claude's SDK directly with no effort argument, relying on Claude's own
    default (unset means "high"); a shared builder defaulting it to chain.py's
    "medium" would have changed the judge's behaviour as a side effect of an
    unrelated refactor. The judge does not call this function at all anymore,
    but the same caution still applies to whatever the next caller is.

    Rate limits (Gemini's free tier has a low RPM cap) are left to each
    integration's own retry/backoff rather than bespoke retry logic here.
    Confirmed rather than assumed, per provider, by inspecting each class's
    `max_retries` field directly (pydantic field defaults, not the __init__
    signature - inspect.signature on these classes shows none of their
    fields, since they're pydantic models with a generated __init__):
      - Gemini (ChatGoogleGenerativeAI): field default 6 (aliased "retries").
      - Anthropic (ChatAnthropic): field default 2.
      - OpenAI (ChatOpenAI): field default None, but None means "don't
        override" - base.py only forwards max_retries to the client when
        it's not None, so an unset field falls through to the underlying
        `openai` SDK client's own default, which is 2
        (openai._constants.DEFAULT_MAX_RETRIES). Effectively nonzero same as
        the other two, just resolved one layer down.
    None of the three needed an explicit max_retries passed at construction.
    """
    spec = spec_for(config)
    key = require_key(config, spec)

    if spec.name == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=spec.model,
            google_api_key=key,
            max_output_tokens=max_tokens,
        )

    if spec.name == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # - api_key passed explicitly, not left to ChatAnthropic's own
        #   ANTHROPIC_API_KEY env lookup: that lookup has no .strip(), so a
        #   value with trailing whitespace (routine for secrets mounted from
        #   files, e.g. a k8s secret volume) would authenticate with a
        #   different byte string than the one require_key just validated.
        kwargs = {"model": spec.model, "max_tokens": max_tokens, "api_key": key}
        if effort is not None:
            kwargs["output_config"] = {"effort": effort}
        return ChatAnthropic(**kwargs)

    if spec.name == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=spec.model,
            api_key=key,
            max_completion_tokens=max_tokens,
        )

    raise ConfigError(f"no builder for provider {spec.name!r}")  # pragma: no cover


# - "json_schema" dodges the bug documented in chain.py: the default method
#   forces tool_choice, which the API rejects whenever thinking is on, and
#   Claude Opus 5 has thinking on by default. OpenAI's structured output
#   supports the same method name. Gemini's integration takes no method
#   argument at all - confirmed by a live call against a real GOOGLE_API_KEY
#   (Task 2's manual Gemini smoke test, run again in Task 8's final
#   verification), not just carried over from the plan.
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
