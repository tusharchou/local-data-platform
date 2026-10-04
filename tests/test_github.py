import logging
from unittest.mock import Mock, patch

import pytest
import requests

from local_data_platform.exceptions import GitHubAPIError
from local_data_platform.github import Item, get_item, get_items

# Mock data representing a GitHub API response for one issue and one PR
MOCK_API_RESPONSE = [
    {
        "number": 1,
        "title": "Test Issue",
        "user": {"login": "test-user"},
        "body": "This is a test issue.",
        "created_at": "2024-01-01T12:00:00Z",
        "closed_at": None,
        "html_url": "https://github.com/test/repo/issues/1",
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "help wanted"}]
    },
    {
        "number": 2,
        "title": "Test PR",
        "user": {"login": "test-user"},
        "body": "This is a test PR.",
        "created_at": "2024-01-02T12:00:00Z",
        "closed_at": "2024-01-03T12:00:00Z",
        "html_url": "https://github.com/test/repo/pull/2",
        "state": "closed",
        "pull_request": {"url": "..."},
        "labels": [{"name": "enhancement"}]
    }
]


@patch('local_data_platform.github.requests.get')
def test_get_items_success(mock_get):
    """Test successful fetching and parsing of GitHub items."""
    # Configure the mock response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = MOCK_API_RESPONSE
    mock_response.links = {}  # No 'next' link, so no pagination
    mock_get.return_value = mock_response

    items = get_items("test-owner", "test-repo")

    assert len(items) == 2
    assert isinstance(items[0], Item)
    assert items[0].number == 1
    assert items[0].title == "Test Issue"
    assert items[0].labels == ["bug", "help wanted"]
    assert not items[0].is_pr
    assert items[1].number == 2
    assert items[1].title == "Test PR"
    assert items[1].labels == ["enhancement"]
    assert items[1].is_pr
    assert items[1].closed_at is not None


@patch('local_data_platform.github.requests.get')
def test_get_items_api_error(mock_get):
    """Test that GitHubAPIError is handled and an empty list is returned."""
    # Configure the mock to raise an exception
    mock_get.side_effect = requests.exceptions.RequestException("API is down")

    items = get_items("test-owner", "test-repo")

    # The function should catch the exception, log it, and return an empty list
    assert items == []


# --- 0.1.1: logging instead of print, get_item, error handling --------------------------------------

MOCK_ISSUE = MOCK_API_RESPONSE[0]


def _response(payload, status_code=200, links=None):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    response.links = links or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} Client Error", response=response
        )
    return response


@patch('local_data_platform.github.requests.get')
def test_get_items_does_not_print(mock_get, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_get.return_value = _response(MOCK_API_RESPONSE)
    get_items("test-owner", "test-repo")
    mock_get.side_effect = requests.exceptions.RequestException("API is down")
    get_items("test-owner", "test-repo")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@patch('local_data_platform.github.requests.get')
def test_get_items_error_is_logged(mock_get, caplog):
    caplog.set_level(logging.ERROR, logger="local_data_platform")
    mock_get.side_effect = requests.exceptions.RequestException("API is down")
    assert get_items("test-owner", "test-repo") == []
    assert "API is down" in caplog.text


@patch('local_data_platform.github.requests.get')
def test_get_items_http_error_returns_empty_list(mock_get):
    mock_get.return_value = _response({"message": "Not Found"}, status_code=404)
    assert get_items("test-owner", "test-repo") == []


@patch('local_data_platform.github.requests.get')
def test_get_items_follows_pagination(mock_get):
    next_url = "https://api.github.com/repositories/1/issues?page=2"
    mock_get.side_effect = [
        _response([MOCK_API_RESPONSE[0]], links={"next": {"url": next_url}}),
        _response([MOCK_API_RESPONSE[1]]),
    ]
    items = get_items("test-owner", "test-repo", state="closed")
    assert [item.number for item in items] == [1, 2]
    first, second = mock_get.call_args_list
    assert first.args[0] == "https://api.github.com/repos/test-owner/test-repo/issues"
    assert first.kwargs["params"]["state"] == "closed"
    assert second.args[0] == next_url
    assert second.kwargs["params"] is None


@patch('local_data_platform.github.requests.get')
def test_get_item_success(mock_get):
    mock_get.return_value = _response(MOCK_ISSUE)

    item = get_item("test-owner", "test-repo", 1)

    assert isinstance(item, Item)
    assert item.number == 1
    assert item.title == "Test Issue"
    assert item.author == "test-user"
    assert item.description == "This is a test issue."
    assert item.labels == ["bug", "help wanted"]
    assert item.created_at.year == 2024 and item.created_at.tzinfo is not None
    assert item.closed_at is None
    assert not item.is_pr
    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "https://api.github.com/repos/test-owner/test-repo/issues/1"
    assert mock_get.call_args.kwargs["timeout"] > 0


@patch('local_data_platform.github.requests.get')
def test_get_item_pull_request(mock_get):
    mock_get.return_value = _response(MOCK_API_RESPONSE[1])
    item = get_item("test-owner", "test-repo", 2)
    assert item.is_pr
    assert item.closed_at is not None


@patch('local_data_platform.github.requests.get')
def test_get_item_http_error_raises(mock_get):
    mock_get.return_value = _response({"message": "Not Found"}, status_code=404)
    with pytest.raises(GitHubAPIError, match="404"):
        get_item("test-owner", "test-repo", 999)


@patch('local_data_platform.github.requests.get')
def test_get_item_connection_error_raises(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError("no network")
    with pytest.raises(GitHubAPIError, match="no network"):
        get_item("test-owner", "test-repo", 1)


@patch('local_data_platform.github.requests.get')
def test_get_item_invalid_json_raises(mock_get):
    response = _response(None)
    response.json.side_effect = ValueError("not json")
    mock_get.return_value = response
    with pytest.raises(GitHubAPIError, match="not JSON"):
        get_item("test-owner", "test-repo", 1)


@patch('local_data_platform.github.requests.get')
def test_get_item_malformed_payload_raises(mock_get):
    mock_get.return_value = _response({"number": 1})
    with pytest.raises(GitHubAPIError, match="payload"):
        get_item("test-owner", "test-repo", 1)
    mock_get.return_value = _response(["not", "an", "object"])
    with pytest.raises(GitHubAPIError, match="payload"):
        get_item("test-owner", "test-repo", 1)


@patch('local_data_platform.github.requests.get')
def test_token_sent_as_header_and_never_logged(mock_get, monkeypatch, caplog):
    token = "ghp_FAKE_TOKEN_1234567890"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    caplog.set_level(logging.DEBUG, logger="local_data_platform")
    mock_get.return_value = _response(MOCK_ISSUE)

    get_item("test-owner", "test-repo", 1)

    assert mock_get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {token}"
    assert token not in caplog.text


@patch('local_data_platform.github.requests.get')
def test_missing_token_logs_warning(mock_get, monkeypatch, caplog):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    caplog.set_level(logging.WARNING, logger="local_data_platform")
    mock_get.return_value = _response(MOCK_ISSUE)

    get_item("test-owner", "test-repo", 1)

    assert "Authorization" not in mock_get.call_args.kwargs["headers"]
    assert "GITHUB_TOKEN not set" in caplog.text
