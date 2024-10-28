from  local_data_platform.store.source import Source
from pathlib import Path    
import json


@dataclass
class GCPCredentials(Path):
    path : str
    project_id : str
    
    def get_project_id(self):
        with open(self.path, 'r') as file:
            data = json.load(file)
        return data.get('project_id')

class GCP(Source):
    """
    A base class for Source Store implementation
    """
    def __init__(self, name: str,path: GCPCredentials):
        self.name = name 
        self.path = path
        super().__init__(self.name,self.path)

