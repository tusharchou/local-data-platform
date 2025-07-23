import requests
from unittest.mock import patch, Mock
from local_data_platform.github import get_items, Item

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