# Feature: A class to interact with the github API

Okay, here's a prompt designed to be given to Gemini Code Assist, building on our previous discussion and leveraging its ability to generate multi-file projects and detailed code.

---

**Gemini Code Assist Prompt:**

"My project needs an object-oriented Python utility to interact with GitHub repositories, specifically for fetching issues and pull requests. I already have a shared low-level API fetching utility located at `scripts/github_api.py` which contains a `fetch_from_github(owner, repo, endpoint, params)` function that handles pagination and authentication via `GITHUB_TOKEN` environment variable.

I need you to create a new Python package and populate it with two classes: `Repo` and `Item`.

**Here are the requirements:**

1.  **Project Structure:**
    * Create a new directory `local_data_platform`.
    * Inside `local_data_platform`, create an empty `__init__.py` file to make it a package.
    * Inside `local_data_platform`, create a new file named `github.py`.

2.  **`local_data_platform/github.py` content:**

    * **Import:** It should import `fetch_from_github` from `scripts.github_api`.

    * **`Repo` Class:**
        * Represents a GitHub repository.
        * `__init__(self, owner: str, name: str)`: Initializes with the repository owner (username or organization) and name.
        * `__repr__(self)`: Provides a helpful string representation.

    * **`Item` Class:**
        * Represents a single GitHub issue or pull request.
        * `__init__(self, data: dict, repo: 'Repo')`:
            * Takes a `data` dictionary (the raw JSON response for an issue/PR from GitHub API) and a `repo` object (an instance of the `Repo` class).
            * Determines `self.type` as either `'issue'` or `'pull_request'` based on the presence of the `'pull_request'` key in `data`.
            * Initializes common attributes directly from `data`: `number`, `title`, `html_url`, `state`, `created_at`, `updated_at`, `closed_at`, `user_login` (from `user.login`), and `labels`.
        * `is_pr(self) -> bool`: Returns `True` if the item is a pull request, `False` otherwise.
        * `is_issue(self) -> bool`: Returns `True` if the item is a regular issue, `False` otherwise.
        * `__repr__(self)`: Provides a helpful string representation including type, number, title (truncated), state, and repo name.
        * `__getattr__(self, name)`: Implement this to allow accessing any key from the underlying `_data` dictionary directly as an attribute (e.g., `item.body` or `item.assignee`). If the attribute doesn't exist in `_data`, raise an `AttributeError`.

        * **`@classmethod fetch_all_items(cls, repo: Repo, status: str = "open") -> list['Item']`**:
            * This is the core fetching method.
            * It should take a `Repo` object and an optional `status` string (defaulting to "open").
            * It must use the imported `fetch_from_github` function, passing `repo.owner`, `repo.name`, the endpoint `"issues"`, and `params={"state": status}`. (Note: The GitHub `/issues` endpoint returns both issues and PRs, which is suitable here).
            * It should then iterate through the raw data returned by `fetch_from_github` and wrap each dictionary in an `Item` object, returning a list of `Item` instances.
            * Include basic type validation for `repo` parameter.

3.  **Example Usage Script (`main.py`):**
    * Create a `main.py` file in the project root (sibling to `local_data_platform` and `scripts`).
    * It should demonstrate how to:
        * Import `Repo` and `Item`.
        * Define `REPO_OWNER` and `REPO_NAME` constants (e.g., "tusharchou", "local-data-platform").
        * Instantiate a `Repo` object.
        * Call `Item.fetch_all_items` to get **open** items.
        * Print the total count of open items.
        * Separate the fetched items into `open_issues` and `open_prs` lists using the `is_issue()` and `is_pr()` methods.
        * Print the counts for open issues and open PRs.
        * Loop through and print the `number`, `title`, `type`, `state`, `user_login`, `created_at` (formatted), and `html_url` for a few (e.g., top 5) issues and PRs.
        * Demonstrate fetching **closed** items as well.
        * Show an example of using the `__getattr__` functionality (e.g., printing `item.body` if available).
    * Include a note about setting the `GITHUB_TOKEN` environment variable for authenticated requests.

Please provide the complete code for `local_data_platform/__init__.py`, `local_data_platform/github.py`, and `main.py`."

---
