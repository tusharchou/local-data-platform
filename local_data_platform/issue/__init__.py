from .. import Repository
from ..logger import log
import requests
from bs4 import BeautifulSoup
from textwrap import fill
import re

logger = log()

"""
class issue is of type Repository
"""


class Issue(Repository):
    def __init__(
            self,
            number: int=1,
            repo_name='local-data-platform',
            owner='tusharchou'
    ) -> str:
        logger.debug(
            f"""
            Loading... Issue Object from {owner} {repo_name} {number}
            """
        )
        self.num = number
        self.project = repo_name
        self.owner = owner
        self.name, self.desc = self._get_github_issue()

    def get(self) -> str:
        return (self.name ,self.desc)

    def _get_github_issue(self):
        url = f"https://github.com/{self.owner}/{self.project}/issues/{self.num}"
        logger.debug(
            f"""
            Pulling Issue Object from {url}
            """
        )

        try:
            response = requests.get(url)
            logger.debug(
                f"""
                Extracting Issue Object from {response}
                """
            )
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

            soup = BeautifulSoup(response.text, 'html.parser')

            """
            <bdi class="js-issue-title markdown-title">PyIceberg Near-Term Roadmap</bdi>
            """
            element = soup.select_one('.js-issue-title').get_text(strip=True)
            # element = soup.select_one('.js-issue-title markdown-title')

            logger.debug(
                f"""
                                        """
            )
            # Extract the issue body
            text = soup.select_one('.js-comment-body').get_text()
            # pattern = re.compile(r'(?<!\s)(?=[A-Z])')
            #
            # split_text = re.split(pattern=pattern, string=text)
            # body = "\n  ".join(split_text)
            body =text
            # body = fill(body_text, width=80)

            body = body if body else 'No body text'
            logger.info(
                f"""
                Reading... issue title {element}
                Fetching... 
                Body: {body}
                Read More: {url}
                """
            )
            return (
                    element,
                    body
            )# Access the 'body' field for the description
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching issue: {e}")
            return None


    def put(self):
        raise Exception