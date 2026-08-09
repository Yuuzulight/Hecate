"""Pipeline configuration, read from the environment.

Values come from real environment variables, or from a .env file if one exists
next to the project root. Anything with a sensible default gets one; the
database password doesn't, because defaulting a credential hides mistakes until
something is already connected to the wrong place.
"""

import os

from dotenv import load_dotenv

from pipeline.exceptions import ConfigError

load_dotenv()


def _int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    """Read an integer setting, falling back to default when it isn't set."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}, got {value}")
    return value


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but not set")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


class Config:
    """Everything the pipeline needs to run, resolved once at startup."""

    def __init__(self) -> None:
        self.db_host = _optional("DB_HOST", "localhost")
        self.db_port = _int("DB_PORT", 5432, maximum=65535)
        self.db_user = _optional("DB_USER", "dataflow")
        self.db_password = _required("DB_PASSWORD")
        self.db_name = _optional("DB_NAME", "hecate")

        # - Both tokens are optional. Without them the APIs still answer, just
        #   at a much lower rate limit, which is fine for a small local run.
        self.github_token = _optional("GITHUB_TOKEN")
        self.gitlab_token = _optional("GITLAB_TOKEN")

        self.npm_registry = _optional("NPM_REGISTRY", "https://registry.npmjs.org")
        self.pypi_registry = _optional("PYPI_REGISTRY", "https://pypi.org")

        # - Bounded at both ends. A stray zero turns a polite run into four
        #   APIs being hammered, and nothing here needs a batch that large.
        # - Off unless asked for. Name matching fails quietly, attaching
        #   attention to the wrong project in a way that looks perfectly
        #   plausible, so it stays opt-in until its measured error rate
        #   justifies it. tools/measure_name_matching.py produces that number.
        self.name_matching = _optional("NAME_MATCHING").lower() in ("1", "true", "yes", "on")

        self.batch_size = _int("BATCH_SIZE", 100, maximum=10_000)
        self.retry_attempts = _int("RETRY_ATTEMPTS", 3, maximum=10)

        # - Empty means no cache, which is a working configuration rather than
        #   a broken one: the retriever answers from PostgreSQL either way.
        self.redis_url = _optional("REDIS_URL")

        # - Optional for the same reason. Without it nothing gets embedded and
        #   the similarity block is simply absent; every structured block still
        #   answers. The embedding job says so and exits non-zero, because a
        #   job that quietly does nothing is the failure this project keeps
        #   finding.
        self.openai_api_key = _optional("OPENAI_API_KEY")

    def __repr__(self) -> str:
        # - Never let the password reach a log line or a traceback.
        return (
            f"Config(db={self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}, "
            f"batch_size={self.batch_size}, retry_attempts={self.retry_attempts}, "
            f"github_token={'set' if self.github_token else 'unset'}, "
            f"gitlab_token={'set' if self.gitlab_token else 'unset'})"
        )
