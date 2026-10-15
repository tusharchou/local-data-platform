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


class ContractError(DataQualityError):
    """Raised when a gold table's contract does not hold, and nothing was written.

    The contract is the ``metadata.docs`` block of the config. A gold table has to describe every column that it
    writes, and may not describe a column that the table does not have. This is a :class:`DataQualityError`, because
    the cure is the same as for a failed check, which is to change the docs or the data, and running the same
    thing again does not help. So ``ldp run`` exits with 3, a workflow marks the task blocked, and nothing retries it.

    Attributes:
        problems: One sentence for each thing that is wrong, which the message lists.
    """

    def __init__(self, message: str, problems=(), report=None):
        super().__init__(message, report)
        self.problems = list(problems)


class GitHubAPIError(LDPError):
    """Raised when a call to the GitHub API fails."""
