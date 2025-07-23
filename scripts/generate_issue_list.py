#!/usr/bin/env python3
"""
Script to generate the 'user_issues.md' page with a filterable list of open GitHub issues.
"""
import os
import re
from typing import List
from local_data_platform.github import get_items, Item

REPO_OWNER = "tusharchou"
REPO_NAME = "local-data-platform"
OUTPUT_PATH = os.path.join('docs', 'user_issues.md')

def parse_labels(labels: List[str]):
    """Parses labels to find status and theme."""
    status = "Planned"  # Default status
    theme = "General"   # Default theme
    for label_name in labels:
        label_name = label_name.lower()
        if label_name.startswith('status:'):
            status = label_name.replace('status:', '').replace('-', ' ').title()
        elif label_name.startswith('theme:'):
            theme = label_name.replace('theme:', '').replace('-', ' ').title()
    return status, theme

def generate_page_content(items: List[Item]):
    """Generates the full Markdown content for the user_issues.md page."""
    header = """# Open Issues

This page lists all open issues in the repository. Use the filters below to sort by status or theme.
"""

    issue_cards = []
    for item in items:
        if item.is_pr:
            continue  # Skip pull requests

        status, theme = parse_labels(item.labels)
        # Truncate body for preview
        body_preview = (item.description or 'No description provided.')
        body_preview = body_preview.replace('\n', ' ').replace('\r', ' ').strip()
        body_preview = re.sub(r'\s+', ' ', body_preview)
        body_preview = (body_preview[:150] + '...') if len(body_preview) > 150 else body_preview

        card = f"""
---
tags:
  - Status - {status}
  - Theme - {theme}
---
### <a href="{item.url}" target="_blank">#{item.number} - {item.title}</a>
*Status: {status} | Theme: {theme}*

> {body_preview}
"""
        issue_cards.append(card)

    if not issue_cards:
        return header + "\nNo open issues found."

    return header + "\n" + "\n---\n".join(issue_cards)


if __name__ == "__main__":
    print("Generating 'Open Issues' page...")
    open_items = get_items(REPO_OWNER, REPO_NAME, state="open")
    page_content = generate_page_content(open_items)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(page_content)
    print(f"Successfully generated page at {OUTPUT_PATH}")