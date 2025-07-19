from local_data_platform.store.source import Source
from pathlib import Path
import json
from local_data_platform import Credentials
from local_data_platform.logger import log

logger = log()

class GCPCredentials(Credentials):
    def __init__(self, path, kwargs):
        """
        todo:
            path,
            type,
            project_id,
            private_key_id,
            private_key,
            client_email,
            client_id,
            auth_uri,
            token_uri,
            auth_provider_x509_cert_url,
            client_x509_cert_url,
            universe_domain
        """

        self.path = path
        self.project_id = kwargs.get("project_id")
        logger.info(f"GCPCredentials initialised with {kwargs}")
        super().__init__(path=self.path, project_id=self.project_id)

    def get_project_id(self):
        with open(self.path, "r") as file:
            data = json.load(file)
        return data.get("project_id")


class GCP(Source):
    """
    A base class for Source Store implementation
    """

    def __init__(self, name: str, path: Path):
        self.name = name
        self.path = path
        logger.info(f"GCP initialised with {self.path}")    
        super().__init__(self.name, self.path)
