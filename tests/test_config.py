"""Config parsing: defaults, required values, and bad input."""

import pytest

from pipeline.config import Config
from pipeline.exceptions import ConfigError
from pipeline.rag.provider_names import PROVIDER_NAMES

# - Config reads the process environment at construction, so each test sets up
#   the variables it cares about and clears the rest.
ALL_VARS = [
    "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME",
    "GITHUB_TOKEN", "GITLAB_TOKEN", "NPM_REGISTRY", "PYPI_REGISTRY",
    "BATCH_SIZE", "RETRY_ATTEMPTS",
    "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "RAG_PROVIDER",
]


@pytest.fixture
def env(monkeypatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PASSWORD", "secret")
    return monkeypatch


def test_defaults_apply_when_nothing_is_set(env):
    config = Config()
    assert config.db_host == "localhost"
    assert config.db_port == 5432
    assert config.db_user == "dataflow"
    assert config.db_name == "hecate"
    assert config.batch_size == 100
    assert config.retry_attempts == 3
    assert config.github_token == ""


def test_environment_overrides_defaults(env):
    env.setenv("DB_HOST", "postgres.hecate.svc")
    env.setenv("BATCH_SIZE", "250")
    config = Config()
    assert config.db_host == "postgres.hecate.svc"
    assert config.batch_size == 250


def test_missing_password_is_an_error(env):
    env.delenv("DB_PASSWORD")
    with pytest.raises(ConfigError, match="DB_PASSWORD"):
        Config()


def test_blank_password_is_an_error(env):
    env.setenv("DB_PASSWORD", "   ")
    with pytest.raises(ConfigError, match="DB_PASSWORD"):
        Config()


def test_non_integer_batch_size_is_an_error(env):
    env.setenv("BATCH_SIZE", "lots")
    with pytest.raises(ConfigError, match="BATCH_SIZE"):
        Config()


def test_zero_retry_attempts_is_an_error(env):
    env.setenv("RETRY_ATTEMPTS", "0")
    with pytest.raises(ConfigError, match="at least 1"):
        Config()


def test_blank_value_falls_back_to_the_default(env):
    env.setenv("DB_HOST", "")
    assert Config().db_host == "localhost"


def test_repr_hides_the_password(env):
    env.setenv("DB_PASSWORD", "hunter2")
    assert "hunter2" not in repr(Config())


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


def test_rag_provider_validates_against_the_shared_name_tuple(env):
    # - Config can't import pipeline.rag.providers (circular import), so its
    #   validation is checked against PROVIDER_NAMES here directly rather
    #   than against providers.SPECS - proving the two independent imports
    #   of the same tuple actually agree on what's valid.
    for name in PROVIDER_NAMES:
        env.setenv("RAG_PROVIDER", name)
        assert Config().rag_provider == name
