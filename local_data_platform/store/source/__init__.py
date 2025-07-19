from local_data_platform.store import Store

class Source(Store):
    """
    A base class for Source Store implementation
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
