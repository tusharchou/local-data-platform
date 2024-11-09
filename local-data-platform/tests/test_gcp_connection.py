import pytest
from unittest.mock import patch, MagicMock
from local_data_platform.store.source.gcp.bigquery import BigQuery, GCPCredentials
from local_data_platform.store.source.json import Json
from pathlib import Path
from pyarrow import Table
import tempfile
import json

@pytest.fixture
def mock_bigquery_client():
    with patch("local_data_platform.store.source.gcp.bigquery.bigquery.Client") as mock_client:
        yield mock_client

@pytest.fixture
def mock_service_account_credentials():
    with patch("local_data_platform.store.source.gcp.bigquery.service_account.Credentials") as mock_credentials:
        yield mock_credentials

@pytest.fixture
def temp_credentials_file():
    credentials_data = {
        "type": "service_account",
        "project_id": "your-project-id",
        "private_key_id": "some-private-key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC...\\n-----END PRIVATE KEY-----\\n",
        "client_email": "your-service-account-email@your-project-id.iam.gserviceaccount.com",
        "client_id": "some-client-id",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/your-project-id.iam.gserviceaccount.com",
    }
    with tempfile.NamedTemporaryFile(delete=False, mode="w") as temp_file:
        json.dump(credentials_data, temp_file)
        temp_file_path = temp_file.name
    yield temp_file_path

@pytest.fixture
def temp_json_file():
    json_data = {
        "query": "SELECT * FROM test_table"
    }
    with tempfile.NamedTemporaryFile(delete=False, mode="w") as temp_file:
        json.dump(json_data, temp_file)
        temp_file_path = temp_file.name
    yield temp_file_path

def test_get_method(mock_service_account_credentials, mock_bigquery_client, temp_credentials_file, temp_json_file):
    mock_service_account_credentials.from_service_account_file.return_value = MagicMock()
    mock_client_instance = mock_bigquery_client.return_value
    mock_query_job = MagicMock()
    mock_query_job.to_dataframe.return_value = MagicMock()
    mock_client_instance.query.return_value = mock_query_job

    credentials = GCPCredentials(path=temp_credentials_file, kwargs={})
    bigquery_instance = BigQuery(name="test", credentials=credentials, path=Path("/tmp"))

    json_instance = Json(path=Path(temp_json_file),name="test")
    query = json_instance.get()["query"]

    # Convert Path to string before passing to BigQuery
    result = bigquery_instance.get(query=str(query))

    assert isinstance(result, Table)
    mock_client_instance.query.assert_called_with(query)