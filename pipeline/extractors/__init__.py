"""Source-specific extractors."""

from pipeline.extractors.base import Extractor
from pipeline.extractors.github import GitHubExtractor

__all__ = ["Extractor", "GitHubExtractor"]
