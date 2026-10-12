"""Tests for the GCP credentials and BigQuery source. Offline: every client is a mock."""

import json
import logging
import sys
from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.store.source.gcp import GCPCredentials
from local_data_platform.store.source.gcp.bigquery import INSTALL_HINT, BigQuery

FAKE_PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nFAKE-SECRET-KEY-MATERIAL-0123456789\n-----END PRIVATE KEY-----\n"
FAKE_PRIVATE_KEY_ID = "fake-private-key-id-abcdef"
SECRET_MARKERS = ("FAKE-SECRET-KEY-MATERIAL", FAKE_PRIVATE_KEY_ID, "BEGIN PRIVATE KEY")


def _key_data(project_id="demo-project"):
    data = {
        "type": "service_account",
        "private_key_id": FAKE_PRIVATE_KEY_ID,
        "private_key": FAKE_PRIVATE_KEY,
        "client_email": "svc@demo-project.iam.gserviceaccount.com",
        "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    if project_id is not None:
        data["project_id"] = project_id
    return data


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key.json"
    path.write_text(json.dumps(_key_data()))
    return path


@pytest.fixture
def query_file(tmp_path):
    path = tmp_path / "query.json"
    path.write_text(json.dumps({"query": "SELECT 1 AS x"}))
    return path


@pytest.fixture
def mock_client():
    """A client whose query().result().to_arrow() returns a real Arrow table."""
    client = MagicMock(name="bigquery.Client")
    result = pa.table({"x": [1, 2, 3]})
    client.query.return_value.result.return_value.to_arrow.return_value = result
    return client


def _assert_no_secrets(text):
    for marker in SECRET_MARKERS:
        assert marker not in text


def _block_google(monkeypatch):
    for name in ("google.cloud.bigquery", "google.oauth2.service_account"):
        monkeypatch.setitem(sys.modules, name, None)


# --- GCPCredentials -------------------------------------------------------------------------------


def test_credentials_read_project_id_from_key_file(key_file):
    creds = GCPCredentials(key_file)
    assert creds.project_id == "demo-project"
    assert creds.path == key_file
    assert creds.get_project_id() == "demo-project"


def test_credentials_relative_path_resolves_against_base_dir(key_file):
    creds = GCPCredentials("key.json", base_dir=key_file.parent)
    assert creds.path == key_file
    assert creds.project_id == "demo-project"


def test_credentials_kwargs_project_id_wins(key_file):
    creds = GCPCredentials(key_file, kwargs={"project_id": "from-kwargs"})
    assert creds.project_id == "from-kwargs"


def test_credentials_empty_kwargs_fall_back_to_key_file(key_file):
    creds = GCPCredentials(path=key_file, kwargs={})
    assert creds.project_id == "demo-project"


def test_credentials_kwargs_without_file_are_allowed(tmp_path):
    creds = GCPCredentials(tmp_path / "missing.json", kwargs={"project_id": "p"})
    assert creds.project_id == "p"


def test_credentials_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        GCPCredentials(tmp_path / "missing.json")


def test_credentials_invalid_json_does_not_leak_contents(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"private_key": "' + FAKE_PRIVATE_KEY.replace("\n", " ") + '", oops')
    with pytest.raises(ConfigError) as excinfo:
        GCPCredentials(path)
    _assert_no_secrets(str(excinfo.value))
    assert excinfo.value.__cause__ is None


def test_credentials_repr_and_str_are_redacted(key_file):
    creds = GCPCredentials(key_file, kwargs=_key_data())
    for text in (repr(creds), str(creds), f"{creds}"):
        _assert_no_secrets(text)
        assert "redacted" in text
        assert "demo-project" in text
    assert not any(FAKE_PRIVATE_KEY in str(value) for value in vars(creds).values())


def test_credentials_never_logged(key_file, caplog):
    caplog.set_level(logging.DEBUG, logger="local_data_platform")
    GCPCredentials(key_file)
    GCPCredentials(key_file, kwargs=_key_data())
    assert "demo-project" in caplog.text
    assert str(key_file) in caplog.text
    _assert_no_secrets(caplog.text)


def test_credentials_without_project_id_warns(tmp_path, caplog):
    path = tmp_path / "key.json"
    path.write_text(json.dumps(_key_data(project_id=None)))
    caplog.set_level(logging.WARNING, logger="local_data_platform")
    creds = GCPCredentials(path)
    assert creds.project_id is None
    assert "no project_id" in caplog.text


# --- BigQuery.get ---------------------------------------------------------------------------------


def test_get_runs_query_exactly_once_and_returns_arrow(key_file, mock_client):
    source = BigQuery("rides", GCPCredentials(key_file), client=mock_client)

    table = source.get("SELECT 1 AS x")

    mock_client.query.assert_called_once_with("SELECT 1 AS x")
    job = mock_client.query.return_value
    job.result.assert_called_once_with()
    job.result.return_value.to_arrow.assert_called_once_with()
    job.to_dataframe.assert_not_called()
    job.to_arrow.assert_not_called()
    assert isinstance(table, pa.Table)
    assert table.column("x").to_pylist() == [1, 2, 3]


def test_get_accepts_query_keyword(key_file, mock_client):
    source = BigQuery(name="rides", credentials=GCPCredentials(key_file), client=mock_client)
    source.get(query="SELECT 2")
    mock_client.query.assert_called_once_with("SELECT 2")


def test_get_reads_query_from_path_when_none_given(key_file, query_file, mock_client):
    source = BigQuery("rides", GCPCredentials(key_file), path=query_file, client=mock_client)
    assert source.path == query_file
    source.get()
    mock_client.query.assert_called_once_with("SELECT 1 AS x")


def test_get_relative_query_path_resolves_against_base_dir(key_file, query_file, mock_client):
    source = BigQuery("rides", GCPCredentials(key_file), path="query.json", client=mock_client,
                      base_dir=query_file.parent)
    source.get()
    mock_client.query.assert_called_once_with("SELECT 1 AS x")


def test_get_without_query_or_path_raises(key_file, mock_client):
    source = BigQuery("rides", GCPCredentials(key_file), client=mock_client)
    with pytest.raises(ConfigError):
        source.get()
    with pytest.raises(ConfigError):
        source.get("   ")
    mock_client.query.assert_not_called()


def test_get_query_file_without_query_key_raises(tmp_path, key_file, mock_client):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"sql": "SELECT 1"}))
    source = BigQuery("rides", GCPCredentials(key_file), path=path, client=mock_client)
    with pytest.raises(ConfigError, match="'query'"):
        source.get()


def test_get_error_propagates_without_retry(key_file, mock_client, caplog):
    mock_client.query.side_effect = RuntimeError("quota exceeded")
    source = BigQuery("rides", GCPCredentials(key_file), client=mock_client)
    caplog.set_level(logging.ERROR, logger="local_data_platform")
    with pytest.raises(RuntimeError, match="quota exceeded"):
        source.get("SELECT 1")
    assert mock_client.query.call_count == 1
    assert "quota exceeded" in caplog.text


def test_get_logs_no_secrets(key_file, mock_client, caplog):
    caplog.set_level(logging.DEBUG, logger="local_data_platform")
    source = BigQuery("rides", GCPCredentials(key_file, kwargs=_key_data()), client=mock_client)
    source.get("SELECT 1")
    repr(source)
    _assert_no_secrets(caplog.text)
    _assert_no_secrets(repr(source))


def test_put_is_not_implemented(key_file, mock_client):
    source = BigQuery("rides", GCPCredentials(key_file), client=mock_client)
    with pytest.raises(NotImplementedError):
        source.put(pa.table({"x": [1]}))


# --- lazy Google imports and client construction --------------------------------------------------


def test_construction_is_lazy_and_missing_google_raises_engine_not_found(key_file, monkeypatch):
    _block_google(monkeypatch)
    source = BigQuery("rides", GCPCredentials(key_file))  # no import, no client yet

    with pytest.raises(EngineNotFound) as excinfo:
        source.get("SELECT 1")
    assert INSTALL_HINT in str(excinfo.value)
    assert 'pip install "local-data-platform[bigquery]"' in str(excinfo.value)


def test_injected_client_needs_no_google(monkeypatch, mock_client):
    _block_google(monkeypatch)
    source = BigQuery("rides", None, client=mock_client)
    assert source.get("SELECT 1").num_rows == 3


def test_client_built_once_from_key_file(key_file, monkeypatch):
    bigquery = pytest.importorskip("google.cloud.bigquery")
    service_account = pytest.importorskip("google.oauth2.service_account")
    fake_google_creds = MagicMock(name="google_credentials", project_id="sa-project")
    from_file = MagicMock(return_value=fake_google_creds)
    fake_client = MagicMock(name="client")
    fake_client.query.return_value.result.return_value.to_arrow.return_value = pa.table({"x": [1]})
    client_cls = MagicMock(return_value=fake_client)
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", from_file)
    monkeypatch.setattr(bigquery, "Client", client_cls)

    source = BigQuery("rides", GCPCredentials(key_file))
    client_cls.assert_not_called()
    source.get("SELECT 1")
    source.get("SELECT 2")

    from_file.assert_called_once_with(str(key_file))
    client_cls.assert_called_once_with(credentials=fake_google_creds, project="demo-project")
    assert fake_client.query.call_count == 2


def test_client_needs_credentials(monkeypatch):
    pytest.importorskip("google.cloud.bigquery")
    source = BigQuery("rides", None)
    with pytest.raises(ConfigError, match="credentials"):
        source.get("SELECT 1")


def test_client_needs_existing_key_file(tmp_path, monkeypatch):
    pytest.importorskip("google.cloud.bigquery")
    creds = GCPCredentials(tmp_path / "gone.json", kwargs={"project_id": "p"})
    source = BigQuery("rides", creds)
    with pytest.raises(ConfigError, match="not found"):
        source.get("SELECT 1")


def test_real_client_built_from_throwaway_key(tmp_path, caplog):
    """Build a real google.cloud.bigquery.Client offline from a freshly generated key."""
    bigquery = pytest.importorskip("google.cloud.bigquery")
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({**_key_data(), "private_key": pem}))
    caplog.set_level(logging.DEBUG, logger="local_data_platform")

    source = BigQuery("rides", GCPCredentials(path))
    client = source.client

    assert isinstance(client, bigquery.Client)
    assert client.project == "demo-project"
    assert source.client is client
    pem_body = pem.splitlines()[1]
    assert pem_body not in caplog.text
    assert pem_body not in repr(source.credentials)
