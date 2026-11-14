#!/usr/bin/env python3
"""Rebuild the pull request table in README.md.

Finds every PR authored by AUTHOR in repositories they do not own, using the
GitHub Search API, and replaces only the content between the PR-TABLE-START and
PR-TABLE-END markers in README.md.

Set GITHUB_TOKEN (or GH_TOKEN) to raise the API rate limit; the workflow passes
the built-in token automatically.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AUTHOR = "simransingh7396-dev"
QUERY = f"author:{AUTHOR} type:pr -user:{AUTHOR}"

README_PATH = Path(__file__).resolve().parent / "README.md"
START_MARKER = "<!-- PR-TABLE-START -->"
END_MARKER = "<!-- PR-TABLE-END -->"

API_ROOT = "https://api.github.com"
PER_PAGE = 100
# The Search API only exposes the first 1000 results per query.
MAX_PAGES = 10
MAX_RETRIES = 3
EMPTY_TEXT = "_No pull requests found yet._"

STATUS_ORDER = {"Merged": 0, "Open": 1, "Closed": 2}


def get_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def api_get(url: str, token: str | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{AUTHOR}-pr-table",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (403, 429) and attempt < MAX_RETRIES:
                wait = 60
                reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
                if reset and reset.isdigit():
                    wait = max(1, int(reset) - int(time.time()) + 1)
                wait = min(wait, 300)
                print(
                    f"GitHub API returned {exc.code}; retrying in {wait}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                last_error = exc
                continue
            raise RuntimeError(f"GitHub API error {exc.code}: {body}") from exc
        except URLError as exc:
            if attempt < MAX_RETRIES:
                wait = 5 * (attempt + 1)
                print(
                    f"Request failed ({exc.reason}); retrying in {wait}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                last_error = exc
                continue
            raise

    raise RuntimeError(f"GitHub API request failed: {last_error}")


def fetch_pull_requests(token: str | None) -> list[dict]:
    search_url = f"{API_ROOT}/search/issues?" + urlencode(
        {"q": QUERY, "per_page": PER_PAGE, "sort": "created", "order": "desc"}
    )

    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = api_get(f"{search_url}&page={page}", token)
        batch = data.get("items", [])
        items.extend(batch)

        total = data.get("total_count", len(items))
        if len(batch) < PER_PAGE or len(items) >= total:
            break
        if len(items) >= 1000:
            print(
                "Reached the 1000-result limit of the GitHub Search API; "
                "some pull requests may be missing.",
                file=sys.stderr,
            )
            break

    pull_requests = []
    for item in items:
        pull_request = item.get("pull_request") or {}
        if not pull_request:
            continue  # Search can return issues; only keep PRs.

        owner_url, _, name = item["repository_url"].rpartition("/")
        owner = owner_url.rpartition("/")[2]
        merged_at = pull_request.get("merged_at")
        state = item.get("state", "open")
        if merged_at:
            status = "Merged"
        elif state == "open":
            status = "Open"
        else:
            status = "Closed"

        pull_requests.append(
            {
                "owner": owner,
                "repo": name,
                "repo_url": f"https://github.com/{owner}/{name}",
                "title": item.get("title", "").strip(),
                "url": item.get("html_url", ""),
                "status": status,
                "created_at": item.get("created_at", ""),
            }
        )

    return pull_requests


def escape_cell(text: str) -> str:
    return (
        " ".join(text.split())
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def format_date(value: str) -> str:
    return value[:10] if value else ""


def parse_timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def render_table(pull_requests: list[dict]) -> str:
    if not pull_requests:
        return EMPTY_TEXT

    groups: dict[str, list[dict]] = defaultdict(list)
    for pr in pull_requests:
        groups[pr["owner"]].append(pr)

    def group_key(owner: str) -> tuple[int, str]:
        has_merged = any(pr["status"] == "Merged" for pr in groups[owner])
        return (0 if has_merged else 1, owner.lower())

    blocks = []
    for owner in sorted(groups, key=group_key):
        rows = sorted(
            groups[owner],
            key=lambda pr: (
                STATUS_ORDER[pr["status"]],
                -parse_timestamp(pr["created_at"]),
                pr["title"],
            ),
        )
        merged = sum(1 for pr in rows if pr["status"] == "Merged")
        parts = [f"### {owner}", ""]
        parts.append(f"{len(rows)} pull request{'s' if len(rows) != 1 else ''}, {merged} merged.")
        parts.append("")
        parts.append("| Repo | Title | Status | Opened |")
        parts.append("| --- | --- | --- | --- |")
        for pr in rows:
            parts.append(
                "| {repo} | {title} | {status} | {opened} |".format(
                    repo=f"[{escape_cell(pr['repo'])}]({pr['repo_url']})",
                    title=f"[{escape_cell(pr['title'])}]({pr['url']})",
                    status=pr["status"],
                    opened=format_date(pr["created_at"]),
                )
            )
        blocks.append("\n".join(parts))

    return "\n\n".join(blocks)


def update_readme(table: str) -> bool:
    with README_PATH.open("r", encoding="utf-8", newline="") as handle:
        original = handle.read()
    newline = "\r\n" if "\r\n" in original else "\n"

    start = original.find(START_MARKER)
    end = original.find(END_MARKER, start + len(START_MARKER)) if start != -1 else -1
    if start == -1 or end == -1:
        raise SystemExit(
            f"README.md must contain both {START_MARKER} and {END_MARKER} markers."
        )

    inner = newline + table.replace("\n", newline) + newline
    updated = original[: start + len(START_MARKER)] + inner + original[end:]

    if updated == original:
        return False
    with README_PATH.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return True


def main() -> int:
    token = get_token()
    if not token:
        print("GITHUB_TOKEN is not set; using unauthenticated rate limits.", file=sys.stderr)

    pull_requests = fetch_pull_requests(token)
    table = render_table(pull_requests)
    changed = update_readme(table)

    merged = sum(1 for pr in pull_requests if pr["status"] == "Merged")
    print(
        f"Found {len(pull_requests)} pull request(s) across "
        f"{len({pr['owner'] for pr in pull_requests})} owner(s), {merged} merged."
    )
    print("README.md updated." if changed else "README.md already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
