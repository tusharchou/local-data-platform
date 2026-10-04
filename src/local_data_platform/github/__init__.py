"""A small client for the GitHub REST API.

``get_items`` lists a repository's issues and pull requests; ``get_item`` fetches
one by number. Set ``GITHUB_TOKEN`` in the environment to authenticate (the token
is sent as a header and never logged); without it GitHub applies its low
unauthenticated rate limit.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

import requests

from local_data_platform.exceptions import GitHubAPIError
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

GITHUB_API_URL = "https://api.github.com"
DEFAULT_TIMEOUT = 30


@dataclass
class Item:
    """Represents a GitHub Issue or Pull Request."""
    number: int
    title: str
    author: str
    description: Optional[str]
    created_at: datetime
    closed_at: Optional[datetime]
    url: str
    is_pr: bool
    labels: List[str]


def _headers(context: str) -> dict:
    """Build request headers, adding ``GITHUB_TOKEN`` when it is set.

    Args:
        context: What the request is for, used in the unauthenticated warning.

    Returns:
        The headers for a GitHub REST API request.
    """
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("GITHUB_TOKEN not set; making an unauthenticated GitHub request (%s)", context)
    return headers


def _request_json(url: str, headers: dict, params: Optional[dict] = None) -> tuple[requests.Response, Any]:
    """GET ``url`` and return the response and its decoded JSON body.

    Raises:
        GitHubAPIError: On a connection error, an HTTP error status or a body that
            is not JSON.
    """
    try:
        response = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response, response.json()
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        raise GitHubAPIError(f"GitHub API returned HTTP {status} for {url}: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise GitHubAPIError(f"Error fetching data from GitHub ({url}): {exc}") from exc
    except ValueError as exc:
        raise GitHubAPIError(f"GitHub API returned a body that is not JSON for {url}") from exc


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_item(raw: dict) -> Item:
    """Convert one issue object from the GitHub API into an :class:`Item`.

    Raises:
        GitHubAPIError: If the object is missing a required field.
    """
    try:
        return Item(
            number=raw["number"],
            title=raw["title"],
            author=(raw.get("user") or {}).get("login", ""),
            description=raw.get("body"),
            created_at=_parse_datetime(raw["created_at"]),
            closed_at=_parse_datetime(raw.get("closed_at")),
            url=raw["html_url"],
            is_pr="pull_request" in raw,
            labels=[label["name"] for label in raw.get("labels") or []],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubAPIError(f"Unexpected GitHub issue payload: {type(exc).__name__}: {exc}") from exc


def _fetch_paginated_data(api_url: str, params: dict, headers: dict) -> List[dict]:
    """Handles pagination for GitHub API requests."""
    all_items = []
    page_num = 1
    while api_url:
        logger.debug("Fetching page %s from %s", page_num, api_url)
        # For subsequent pages, params are already in the URL, so we pass None
        current_params = params if page_num == 1 else None
        response, fetched_items = _request_json(api_url, headers, current_params)
        if not fetched_items:
            break

        all_items.extend(fetched_items)

        if 'next' in response.links:
            api_url = response.links['next']['url']
            page_num += 1
        else:
            api_url = None
    return all_items


def get_items(repo_owner: str, repo_name: str, state: str = "all") -> List[Item]:
    """Fetch Issues and Pull Requests from a GitHub repository.

    Args:
        repo_owner: The owner of the repository.
        repo_name: The name of the repository.
        state: The state of the items to fetch ('open', 'closed', 'all').

    Returns:
        A list of Item objects. For backward compatibility, a failed request is
        logged and returns an empty list instead of raising.
    """
    headers = _headers(f"items in '{state}' state")
    api_url = f"{GITHUB_API_URL}/repos/{repo_owner}/{repo_name}/issues"
    params = {"state": state, "per_page": 100, "sort": "updated", "direction": "desc"}

    try:
        raw_items = _fetch_paginated_data(api_url, params, headers)
    except GitHubAPIError as e:
        logger.error("Could not list GitHub items for %s/%s: %s", repo_owner, repo_name, e)
        return []

    return [_parse_item(raw_item) for raw_item in raw_items]


def get_item(repo_owner: str, repo_name: str, number: int) -> Item:
    """Fetch one Issue or Pull Request by number.

    Uses ``GET /repos/{owner}/{repo}/issues/{number}``, which also returns pull
    requests (``Item.is_pr`` tells them apart).

    Args:
        repo_owner: The owner of the repository.
        repo_name: The name of the repository.
        number: The issue or pull request number.

    Returns:
        The matching :class:`Item`.

    Raises:
        GitHubAPIError: If the request fails, GitHub returns an error status (for
            example 404 for an unknown number) or the payload is malformed.
    """
    headers = _headers(f"issue #{number}")
    api_url = f"{GITHUB_API_URL}/repos/{repo_owner}/{repo_name}/issues/{number}"
    logger.debug("Fetching GitHub item %s/%s#%s", repo_owner, repo_name, number)
    _, raw_item = _request_json(api_url, headers)
    if not isinstance(raw_item, dict):
        raise GitHubAPIError(f"Unexpected GitHub issue payload for {repo_owner}/{repo_name}#{number}")
    return _parse_item(raw_item)
