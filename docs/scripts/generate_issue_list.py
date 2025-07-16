import requests
import os

def fetch_github_issues(repo_owner, repo_name):
    """Fetches open issues from a GitHub repository using the GitHub API."""
    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues?state=open"
    access_token = os.environ.get("GITHUB_TOKEN")
    headers = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching issues from GitHub: {e}")
        return None

def format_issues_as_markdown(issues):
    """Formats a list of GitHub issues into a Markdown list."""
    if not issues:
        return "No open issues found."

    markdown_output = "\n".join(
        f"-   [#{issue['number']} {issue['title']}]({issue['html_url']})"
        for issue in issues
    )
    return markdown_output

if __name__ == "__main__":
    repo_owner = "tusharchou"
    repo_name = "local-data-platform"

    issues = fetch_github_issues(repo_owner, repo_name)

    if issues:
        markdown_content = format_issues_as_markdown(issues)
        output_path = "docs/user_issues.md"
        with open(output_path, "w") as f:
            f.write(f"# Open Issues\n\n{markdown_content}")
        print(f"Successfully wrote issue list to {output_path}")
    else:
        print("Failed to retrieve issues. Check your internet connection and GitHub API limits.")