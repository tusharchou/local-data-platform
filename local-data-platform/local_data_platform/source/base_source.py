from abc import ABC

class SourceFile(ABC):
    """
    A base class for SourceFile implementation

    Args:
        location (str): A URI or a path to a local file

    Attributes:
        location (str): The path to SourceFile instance
        exists (boo): w

    """
    _name: str

    def __init__(self, location: str):
        self