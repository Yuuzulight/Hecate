"""Source-specific extractors."""

from pipeline.extractors.base import Extractor
from pipeline.extractors.github import GitHubExtractor
from pipeline.extractors.gitlab import GitLabExtractor
from pipeline.extractors.npm import NpmExtractor
from pipeline.extractors.pypi import PyPiExtractor

__all__ = ["Extractor", "GitHubExtractor", "GitLabExtractor", "NpmExtractor", "PyPiExtractor"]
