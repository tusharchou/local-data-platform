import pytest
import tempfile
import json
from unittest.mock import patch, MagicMock
from local_data_platform.source.gcp.gcp_connection import GCPBigQueryConnection

@pytest.fixture
def mock_bigquery_client():
    with patch('local_data_platform.source.gcp.gcp_connection.bigquery.Client') as mock_client:
        yield mock_client

@pytest.fixture
def mock_service_account_credentials():
    with patch('local_data_platform.source.gcp.gcp_connection.service_account.Credentials') as mock_credentials:
        yield mock_credentials

@pytest.fixture
def temp_credentials_file():
    """
Fixture that creates a temporary JSON file containing Google Cloud Platform (GCP) service account credentials.

This fixture generates a temporary file with mock GCP service account credentials and yields the file path.
The file is not deleted automatically after the test, allowing for inspection if needed.

Returns:
    str: The file path to the temporary credentials JSON file.
"""
    credentials_data = {
        "type": "service_account",
        "project_id": "your-project-id",
        "private_key_id": "some-private-key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC...\n-----END PRIVATE KEY-----\n",
        "client_email": "your-service-account-email@your-project-id.iam.gserviceaccount.com",
        "client_id": "some-client-id",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/your-service-account-email%40your-project-id.iam.gserviceaccount.com"
    }
    with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.json') as temp_file:
        json.dump(credentials_data, temp_file)
        temp_file_path = temp_file.name
    yield temp_file_path

def test_query(mock_service_account_credentials, mock_bigquery_client, temp_credentials_file):
    """
    Test the `query` method of the `GCPBigQueryConnection` class.

    This test verifies that the `query` method correctly executes a SQL query and returns the expected results.
    It mocks the service account credentials and BigQuery client to simulate the interaction with Google Cloud Platform.

    Args:
        mock_service_account_credentials (MagicMock): Mocked service account credentials.
        mock_bigquery_client (MagicMock): Mocked BigQuery client.
        temp_credentials_file (str): Path to a temporary credentials file.

    Steps:
    1. Mock the service account credentials and BigQuery client.
    2. Initialize the `GCPBigQueryConnection` with the temporary credentials file and project ID.
    3. Execute the `query` method with a sample SQL query.
    4. Verify that the results match the expected output.
    5. Ensure that the `query` method was called with the correct query string.

    Running the Tests:
    pytest tests/test_gcp_connection.py
    """
    # Mock the credentials and client
    mock_service_account_credentials.from_service_account_file.return_value = MagicMock()
    mock_client_instance = mock_bigquery_client.return_value
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = [
        {'column1': 'value1', 'column2': 'value2'}
    ]
    mock_client_instance.query.return_value = mock_query_job

    # Initialize the GCPBigQueryConnection
    connection = GCPBigQueryConnection(temp_credentials_file, 'your-project-id')

    # Execute the query method
    result = connection.query('SELECT * FROM dataset.table')

    # Verify the results
    assert len(result) == 1
    assert result[0]['column1'] == 'value1'
    assert result[0]['column2'] == 'value2'

    # Verify that the query method was called with the correct query string
    mock_client_instance.query.assert_called_once_with('SELECT * FROM dataset.table')