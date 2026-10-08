"""Exceptions raised by local-data-platform.

Every exception derives from :class:`LDPError`, so callers can catch the whole
family with one ``except`` clause.
"""


class LDPError(Exception):
    """Base class for every error raised by local-data-platform."""


class ConfigError(LDPError):
    """Raised when a dataset config is missing a key or has an invalid value."""


class TableNotFound(LDPError):
    """Raised when accessing a table that doesn't exist."""


class PipelineNotFound(LDPError):
    """Raised when no pipeline is registered for a source and target pair."""


class EngineNotFound(LDPError):
    """Raised when an engine is not supported or its optional dependency is missing."""


class PlanNotFound(LDPError):
    """Raised when an issue doesn't have a resolution estimate."""


class DataQualityError(LDPError):
    """Raised when a data quality check fails and the pipeline is set to fail fast.

    The failing :class:`~local_data_platform.quality.QualityReport` is available
    as ``error.report``.
    """

    def __init__(self, message: str, report=None):
        super().__init__(message)
        self.report = report


class GitHubAPIError(LDPError):
    """Raised when a call to the GitHub API fails."""
