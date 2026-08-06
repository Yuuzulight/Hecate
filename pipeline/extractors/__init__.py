"""Source-specific extractors."""

from pipeline.extractors.base import Extractor
from pipeline.extractors.github import GitHubExtractor
from pipeline.extractors.npm import NpmExtractor

__all__ = ["Extractor", "GitHubExtractor", "NpmExtractor"]
