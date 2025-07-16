import os
import pytest
from docs.scripts.create_github_issue import create_github_issue


def test_create_github_issue():
    """
    Test the create_github_issue function.

    This test attempts to create an issue and asserts that the creation is successful.
    It checks for the presence of the GITHUB_TOKEN environment variable and verifies
    that the function returns True upon successful issue creation.
    """
    repo_owner = "tusharchou"  # Replace with your GitHub username
    repo_name = "local-data-platform"  # Replace with your repository name
    issue_title = "Test issue creation from pytest"
    issue_body = "This is a test issue created via pytest. Please disregard."

    # Skip the test if GITHUB_TOKEN is not set
    if not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("GITHUB_TOKEN environment variable not set.")

    # Attempt to create the issue
    issue_created = create_github_issue(repo_owner, repo_name, issue_title, issue_body)

    # Assert that the issue was created successfully
    assert issue_created is True, "Issue creation failed. Check the script output for errors."

# This allows you to run the test with pytest
if __name__ == "__main__":
    # To run this as a standalone script:
    # 1. Ensure you have pytest installed: pip install pytest
    # 2. Run the script: pytest test_create_github_issue.py