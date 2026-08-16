"""Exceptions raised by the pipeline.

Everything inherits from HecateError so a caller that doesn't care which stage
failed can catch the one type.
"""


class HecateError(Exception):
    """Base for every error the pipeline raises deliberately."""


class ConfigError(HecateError):
    """Configuration is missing or unusable."""


class ExtractError(HecateError):
    """A source could not be read."""


class TransformError(HecateError):
    """A record could not be normalised to the standard schema."""


class LoadError(HecateError):
    """Writing to the database failed."""


class EmbeddingError(HecateError):
    """Embeddings could not be produced or stored."""


class ForecastError(HecateError):
    """A forecast could not be produced or stored."""
