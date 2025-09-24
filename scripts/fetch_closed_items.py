#!/usr/bin/env python3
"""
Script to fetch and display closed Pull Requests and Issues from a GitHub repository
and write them to a Markdown file.
"""
import os
from typing import List
from local_data_platform.github import get_items, Item

# --- Configuration ---
REPO_OWNER = "tusharchou"
REPO_NAME = "local-data-platform"
OUTPUT_PATH = os.path.join('docs', 'closed_items.md')
# ---------------------

def format_items_as_markdown(items: List[Item], item_type: str) -> str:
    """Formats a list of GitHub items into a Markdown list."""
    if not items:
        return f"No closed {item_type} found or failed to fetch."

    markdown_list = []
    for item in items:
        author = item.author
        if item.closed_at:
            closed_date = item.closed_at.strftime('%Y-%m-%d')
            date_info = f" on {closed_date} "
        else:
            date_info = ""
        markdown_list.append(f"- **[{item_type} #{item.number}]({item.url})**: {item.title} (closed{date_info}by @{author})")
    return "\n".join(markdown_list)

def main():
    """Main function to fetch closed items and write them to a Markdown file."""
    print("Fetching all closed items...")

    all_closed_items = get_items(REPO_OWNER, REPO_NAME, state="closed")
    closed_prs = [item for item in all_closed_items if item.is_pr]
    closed_issues = [item for item in all_closed_items if not item.is_pr]

    content = "# All Closed Items\n\n"
    content += "This page lists all closed Pull Requests and Issues, sorted by most recently updated.\n\n"
    content += "## Closed Pull Requests\n\n"
    content += format_items_as_markdown(closed_prs, "PR")
    content += "\n\n## Closed Issues\n\n"
    content += format_items_as_markdown(closed_issues, "Issue")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"Successfully generated closed items report at {OUTPUT_PATH}")

if __name__ == "__main__":
    main()