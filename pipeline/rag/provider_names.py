"""The three provider names, in exactly one place.

pipeline/config.py validates RAG_PROVIDER against this tuple; providers.py's
SPECS and _STRUCTURED_OUTPUT_METHOD are keyed by it; evaluation.py's judge
construction branches on it. Split into its own module because config.py
can't import pipeline/rag/providers.py to share its copy without a circular
import - providers.py already imports Config. This module imports nothing,
so everything else can import it.
"""

PROVIDER_NAMES = ("gemini", "anthropic", "openai")
