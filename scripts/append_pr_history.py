#!/usr/bin/env python3
"""
Script to append PR history and directory changes to docs/PR_HISTORY.md after a merge.
Intended for use in CI/CD or as a manual post-merge tool.
"""
import os
import subprocess
import sys
from datetime import datetime

PR_HISTORY_PATH = os.path.join('docs', 'PR_HISTORY.md')

def get_env_var(name, fallback=None):
    return os.environ.get(name, fallback)

def get_last_merge_commit():
    # Get the last merge commit hash
    result = subprocess.run(['git', 'log', '--merges', '-1', '--pretty=%H'], capture_output=True, text=True)
    return result.stdout.strip()

def get_parent_commit(merge_commit):
    # Get the first parent of the merge commit
    result = subprocess.run(['git', 'rev-parse', f'{merge_commit}^1'], capture_output=True, text=True)
    return result.stdout.strip()

def get_changed_dirs(parent, merge):
    # Get changed directories between two commits
    result = subprocess.run(['git', 'diff', '--dirstat=files,0', parent, merge], capture_output=True, text=True)
    dirs = set()
    for line in result.stdout.splitlines():
        if line.strip():
            dir_path = line.split()[-1].rstrip('/')
            dirs.add(f'/{dir_path}/')
    return sorted(dirs)

def append_pr_history(pr_number, pr_title, merger, date_merged, description, changed_dirs):
    with open(PR_HISTORY_PATH, 'a') as f:
        f.write(f"\n### PR #{pr_number}: {pr_title}\n")
        f.write(f"**Merged By:** {merger} on {date_merged}\n")
        f.write(f"**Description:** {description}\n")
        f.write(f"**Directory Changes:**\n")
        for d in changed_dirs:
            f.write(f"* {d}\n")

if __name__ == '__main__':
    # These would be set by your CI/CD or passed as env vars/args
    pr_number = get_env_var('PR_NUMBER', 'UNKNOWN')
    pr_title = get_env_var('PR_TITLE', 'No title provided')
    merger = get_env_var('PR_MERGER', 'UNKNOWN')
    date_merged = datetime.now().strftime('%Y-%m-%d')
    description = get_env_var('PR_DESCRIPTION', 'No description provided')

    merge_commit = get_last_merge_commit()
    parent_commit = get_parent_commit(merge_commit)
    changed_dirs = get_changed_dirs(parent_commit, merge_commit)

    append_pr_history(pr_number, pr_title, merger, date_merged, description, changed_dirs)
    print(f"PR history updated in {PR_HISTORY_PATH}")
