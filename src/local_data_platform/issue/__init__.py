"""A GitHub issue of a repository, read through the GitHub REST API.

The issue is fetched lazily, on first access to ``name``, ``desc``, ``item`` or
``get()``, through :func:`local_data_platform.github.get_item`. Constructing an
``Issue`` makes no network call.
"""

from local_data_platform import Base, github
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

NO_BODY_TEXT = "No body text"


class Issue(Base):
    """A GitHub issue.

    Args:
        number: The issue number.
        repo_name: The repository name.
        owner: The repository owner.

    Raises:
        ValueError: If ``number`` is not a positive integer.
    """

    def __init__(self, number: int = 1, repo_name: str = "local-data-platform", owner: str = "tusharchou"):
        super().__init__()
        number = int(number)
        if number < 1:
            raise ValueError(f"issue number must be a positive integer, got {number}")
        self.number = number
        self.repo_name = repo_name
        self.owner = owner
        self._item: github.Item | None = None

    # Pre-0.1.1 attribute names.
    @property
    def num(self) -> int:
        return self.number

    @property
    def project(self) -> str:
        return self.repo_name

    @property
    def url(self) -> str:
        """The issue's page on github.com."""
        return f"https://github.com/{self.owner}/{self.repo_name}/issues/{self.number}"

    @property
    def item(self) -> github.Item:
        """The issue as a :class:`local_data_platform.github.Item`, fetched once and cached.

        Raises:
            GitHubAPIError: If the GitHub API request fails.
        """
        if self._item is None:
            logger.debug("Fetching issue %s/%s#%s", self.owner, self.repo_name, self.number)
            self._item = github.get_item(self.owner, self.repo_name, self.number)
            logger.info("Read issue %s/%s#%s: %s", self.owner, self.repo_name, self.number, self._item.title)
        return self._item

    @property
    def name(self) -> str:
        """The issue title."""
        return self.item.title

    @property
    def desc(self) -> str:
        """The issue body, or ``"No body text"`` when it is empty."""
        return self.item.description or NO_BODY_TEXT

    def get(self) -> tuple[str, str]:
        """Return the issue's ``(title, body)``.

        Raises:
            GitHubAPIError: If the GitHub API request fails.
        """
        return self.name, self.desc

    def put(self, *args, **kwargs):
        """Issues are read-only.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("Issue is read-only; updating GitHub issues is not supported")

    def __repr__(self) -> str:
        return f"Issue(number={self.number}, repo_name={self.repo_name!r}, owner={self.owner!r})"
