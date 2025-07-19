import os
import requests


def fetch_github_issues(repo_owner: str, repo_name: str) -> list:
    """
    Fetches open issues from a GitHub repository using the GitHub API.
    Handles authentication via GITHUB_TOKEN for higher rate limits.
    """
    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues?state=open"
    access_token = os.environ.get("GITHUB_TOKEN")
    headers = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching issues from GitHub: {e}")
        return []


def main():
    """Main function to generate the issues markdown file."""
    repo_owner = "tusharchou"
    repo_name = "local-data-platform"
    output_path = "docs/includes/user_issues_list.md"

    issues = fetch_github_issues(repo_owner, repo_name)
    markdown_content = "### Currently Open Issues\n\n"
    if issues:
        markdown_content += "\n".join(
            f"- [#{issue['number']} - {issue['title']}]({issue['html_url']})"
            for issue in issues
        )
    else:
        markdown_content += "No open issues found or failed to fetch issues."

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    print(f"Successfully generated issue list at {output_path}")


if __name__ == "__main__":
    main()