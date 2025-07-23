import os
import requests
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List
from local_data_platform.exceptions import GitHubAPIError

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

def _fetch_paginated_data(api_url: str, params: dict, headers: dict) -> List[dict]:
    """Handles pagination for GitHub API requests."""
    all_items = []
    page_num = 1
    while api_url:
        print(f"Fetching page {page_num} from {api_url}...")
        try:
            # For subsequent pages, params are already in the URL, so we pass None
            current_params = params if page_num == 1 else None
            response = requests.get(api_url, headers=headers, params=current_params)
            response.raise_for_status()

            fetched_items = response.json()
            if not fetched_items:
                break

            all_items.extend(fetched_items)

            if 'next' in response.links:
                api_url = response.links['next']['url']
                page_num += 1
            else:
                api_url = None
        except requests.exceptions.RequestException as e:
            raise GitHubAPIError(f"Error fetching data from GitHub: {e}") from e
    return all_items

def get_items(repo_owner: str, repo_name: str, state: str = "all") -> List[Item]:
    """
    Fetches Issues and Pull Requests from a GitHub repository.

    Args:
        repo_owner: The owner of the repository.
        repo_name: The name of the repository.
        state: The state of the items to fetch ('open', 'closed', 'all').

    Returns:
        A list of Item objects.
    """
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print(f"Warning: GITHUB_TOKEN not set. Making unauthenticated requests to fetch items in '{state}' state.")

    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues"
    params = {"state": state, "per_page": 100, "sort": "updated", "direction": "desc"}

    try:
        raw_items = _fetch_paginated_data(api_url, params, headers)
    except GitHubAPIError as e:
        print(e)
        return []
 
    items = []
    for raw_item in raw_items:
        # Safely parse datetime strings
        created_at = datetime.fromisoformat(
            raw_item['created_at'].replace('Z', '+00:00')
        )
        closed_at = None
        if raw_item.get('closed_at'):
            closed_at = datetime.fromisoformat(
                raw_item['closed_at'].replace('Z', '+00:00')
            )
 
        item = Item(
            number=raw_item['number'],
            title=raw_item['title'],
            author=raw_item['user']['login'],
            description=raw_item.get('body'),
            created_at=created_at,
            closed_at=closed_at,
            url=raw_item['html_url'],
            is_pr='pull_request' in raw_item,
            labels=[label['name'] for label in raw_item.get('labels', [])]
        )
        items.append(item)
 
    return items