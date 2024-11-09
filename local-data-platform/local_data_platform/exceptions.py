class TableNotFound(Exception):
    """Raised when accessing a table that doesn't exist"""


class PipelineNotFound(Exception):
    """Raised when running a pipeline that doesn't exist"""


class EngineNotFound(Exception):
    """Raised when engine is not supported"""

class PlanNotFound(Exception):
    """Raised when issue doesn't have resolution estimate"""