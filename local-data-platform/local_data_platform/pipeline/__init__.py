'''
A data pipline captures data from the source and stores it your target storage
'''
from .. import Flow, Config
from ..store.source import Source
from ..store.target import Target
from ..catalog import Catalog


class Pipeline(Flow):

    def __init__(self, config: Config, *args, **kwargs):
        self.config = config
        self.source = Source(**config.metadata['source'])
        self.target = Target(**config.metadata['target'])
        super().__init__(*args, **kwargs)

