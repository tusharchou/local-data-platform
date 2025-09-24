#!/usr/bin/env python3
"""
Script to append PR history and directory changes to docs/PR_HISTORY.md after a merge.
Intended for use in CI/CD or as a manual post-merge tool.
"""
import os
import subprocess
from datetime import datetime

PR_HISTORY_PATH = os.path.join('docs', 'PR_HISTORY.md')

def get_env_var(name, fallback=None):
    return os.environ.get(name, fallback)

def get_last_merge_commit():
    result = subprocess.run(['git', 'log', '--merges', '-1', '--pretty=%H'], capture_output=True, text=True, check=True)
    return result.stdout.strip()

def get_changed_dirs(base, head):
    result = subprocess.run(['git', 'diff', '--dirstat=files,0', base, head], capture_output=True, text=True, check=True)
    dirs = {f"/{line.split()[-1].rstrip('/')}/" for line in result.stdout.splitlines() if line.strip()}
    return sorted(list(dirs))

def append_pr_history(pr_number, pr_title, merger, date_merged, description, changed_dirs):
    with open(PR_HISTORY_PATH, 'a', encoding='utf-8') as f:
        f.write(f"\n### PR #{pr_number}: {pr_title}\n\n")
        f.write(f"**Merged By:** {merger} on {date_merged}\n\n")
        f.write(f"**Description:**\n\n```\n{description}\n```\n\n")
        f.write(f"**Directory Changes:**\n\n")
        for d in changed_dirs:
            f.write(f"- `{d}`\n")
        f.write("\n---\n")

if __name__ == '__main__':
    merge_commit = get_last_merge_commit()
    # The second parent of a merge commit is the head of the merged branch
    parent_commit = f'{merge_commit}^2'
    changed_dirs = get_changed_dirs(parent_commit, merge_commit)

    append_pr_history(
        pr_number=get_env_var('PR_NUMBER', 'N/A'),
        pr_title=get_env_var('PR_TITLE', 'No title provided'),
        merger=get_env_var('PR_MERGER', 'N/A'),
        date_merged=datetime.now().strftime('%Y-%m-%d'),
        description=get_env_var('PR_DESCRIPTION', 'No description provided'),
        changed_dirs=changed_dirs
    )
    print(f"PR history successfully updated in {PR_HISTORY_PATH}")