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
    # - Switched from gemini-2.5-flash: it 404s for this account as of this
    #   session ("no longer available to new users" - retired), confirmed
    #   against a real key. gemini-3.5-flash is confirmed available and
    #   working against the same key.
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
#   argument at all - not yet confirmed by a live call (see Task 2's report:
#   the manual Gemini smoke test is deferred pending a real GOOGLE_API_KEY),
#   so this is carried over from the plan rather than independently verified.
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
