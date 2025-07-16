from .. import Repository
from ..logger import log
import requests
from bs4 import BeautifulSoup
from textwrap import fill
import re

logger = log()

"""
A class to represent a GitHub issue.
"""


class Issue(Repository):
    """Represents a GitHub issue with a title and description."""

    def __init__(self, number: int, project: str, owner: str, name: str, desc: str):
        """Initializes the Issue object. It is recommended to use the `from_github` classmethod to create instances."""
        self.num = number
        self.project = project
        self.owner = owner
        self.name = name
        self.desc = desc

    def get(self) -> tuple[str, str]:
        """Returns the name and description of the issue."""
        return (self.name, self.desc)

    @classmethod
    def from_github(
        cls,
        number: int = 1,
        repo_name: str = "local-data-platform",
        owner: str = "tusharchou",
    ):
        """
        Fetches an issue from GitHub and creates an Issue object.

        Args:
            number (int): The issue number.
            repo_name (str): The name of the repository.
            owner (str): The owner of the repository.

        Returns:
            An instance of the Issue class.

        Raises:
            requests.exceptions.RequestException: If there is an error fetching the issue.
        """
        url = f"https://github.com/{owner}/{repo_name}/issues/{number}"
        logger.debug(f"Pulling Issue Object from {url}")

        try:
            response = requests.get(url)
            logger.debug(f"Extracting Issue Object from {response}")
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

            soup = BeautifulSoup(response.text, 'html.parser')
            title = soup.select_one('.js-issue-title').get_text(strip=True)
            body = soup.select_one('.js-comment-body').get_text(strip=True) or "No body text"
            logger.info(f"Successfully fetched issue #{number}: {title}")
            return cls(number=number, project=repo_name, owner=owner, name=title, desc=body)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching issue: {e}")
            raise

    def put(self):
        raise NotImplementedError("The 'put' method is not implemented for the Issue class.")