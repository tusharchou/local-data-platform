"""Tests for ``local_data_platform.issue.Issue``. Offline: GitHub is mocked."""

import logging
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
import requests

from local_data_platform.exceptions import GitHubAPIError
from local_data_platform.github import Item
from local_data_platform.issue import NO_BODY_TEXT, Issue


def _item(number=7, title="Add DuckDB engine", description="Query Iceberg with SQL."):
    return Item(
        number=number,
        title=title,
        author="someone",
        description=description,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
        url=f"https://github.com/tusharchou/local-data-platform/issues/{number}",
        is_pr=False,
        labels=["enhancement"],
    )


@patch("local_data_platform.github.get_item")
def test_construction_makes_no_network_call(mock_get_item):
    issue = Issue(number=7)
    mock_get_item.assert_not_called()
    assert issue.number == 7
    assert issue.num == 7
    assert issue.repo_name == "local-data-platform"
    assert issue.project == "local-data-platform"
    assert issue.owner == "tusharchou"
    assert issue.url == "https://github.com/tusharchou/local-data-platform/issues/7"
    assert repr(issue) == "Issue(number=7, repo_name='local-data-platform', owner='tusharchou')"


def test_defaults():
    issue = Issue()
    assert (issue.number, issue.repo_name, issue.owner) == (1, "local-data-platform", "tusharchou")


@patch("local_data_platform.github.get_item")
def test_get_returns_title_and_body_and_fetches_once(mock_get_item):
    mock_get_item.return_value = _item()
    issue = Issue(number=7, repo_name="repo", owner="me")

    assert issue.get() == ("Add DuckDB engine", "Query Iceberg with SQL.")
    assert issue.name == "Add DuckDB engine"
    assert issue.desc == "Query Iceberg with SQL."
    assert issue.item.labels == ["enhancement"]
    mock_get_item.assert_called_once_with("me", "repo", 7)


@pytest.mark.parametrize("body", [None, ""])
@patch("local_data_platform.github.get_item")
def test_empty_body_becomes_placeholder(mock_get_item, body):
    mock_get_item.return_value = _item(description=body)
    assert Issue(7).get() == ("Add DuckDB engine", NO_BODY_TEXT)


@patch("local_data_platform.github.get_item")
def test_errors_raise_github_api_error(mock_get_item):
    mock_get_item.side_effect = GitHubAPIError("GitHub API returned HTTP 404")
    issue = Issue(404)
    with pytest.raises(GitHubAPIError, match="404"):
        issue.get()
    with pytest.raises(GitHubAPIError):
        issue.name
    with pytest.raises(GitHubAPIError):
        issue.desc


@patch("local_data_platform.github.get_item")
def test_failed_fetch_is_retried_on_next_access(mock_get_item):
    mock_get_item.side_effect = [GitHubAPIError("rate limited"), _item()]
    issue = Issue(7)
    with pytest.raises(GitHubAPIError):
        issue.get()
    assert issue.get()[0] == "Add DuckDB engine"
    assert mock_get_item.call_count == 2


@patch("local_data_platform.github.requests.get")
def test_uses_rest_api_not_html(mock_get, caplog):
    response = Mock()
    response.status_code = 200
    response.links = {}
    response.json.return_value = {
        "number": 3,
        "title": "PyIceberg Near-Term Roadmap",
        "user": {"login": "tusharchou"},
        "body": "Roadmap body",
        "created_at": "2024-01-01T12:00:00Z",
        "closed_at": None,
        "html_url": "https://github.com/tusharchou/local-data-platform/issues/3",
        "labels": [],
    }
    mock_get.return_value = response
    caplog.set_level(logging.INFO, logger="local_data_platform")

    assert Issue(3).get() == ("PyIceberg Near-Term Roadmap", "Roadmap body")

    url = mock_get.call_args.args[0]
    assert url == "https://api.github.com/repos/tusharchou/local-data-platform/issues/3"
    assert "PyIceberg Near-Term Roadmap" in caplog.text


@patch("local_data_platform.github.requests.get")
def test_http_error_through_github_module_raises(mock_get):
    response = Mock()
    response.status_code = 404
    response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Client Error", response=response)
    mock_get.return_value = response
    with pytest.raises(GitHubAPIError, match="404"):
        Issue(99999).get()


def test_put_is_not_supported():
    with pytest.raises(NotImplementedError):
        Issue(1).put()


@pytest.mark.parametrize("number", [0, -3])
def test_invalid_number_raises(number):
    with pytest.raises(ValueError):
        Issue(number)
