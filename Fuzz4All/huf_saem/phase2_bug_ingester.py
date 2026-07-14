"""Ingest closed bug reports from GitHub and/or local CSV files."""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional

import requests


class BugIngester:
    def __init__(
        self,
        github_repo: Optional[str] = None,
        token: Optional[str] = None,
        csv_path: Optional[str] = None,
    ) -> None:
        self.github_repo = github_repo
        self.token = token
        self.csv_path = csv_path

    def load_from_csv(self) -> List[Dict]:
        if not self.csv_path or not os.path.exists(self.csv_path):
            return []
        records: List[Dict] = []
        with open(self.csv_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalize header keys so spaced headers ("short desc",
                # "reported to") match the underscore names we look up below.
                # The shipped bugs/*.csv files use spaces; without this every
                # row parses empty and Phase 2 silently synthesizes 0 mutators.
                norm = {
                    (k.strip().lower().replace(" ", "_") if k else k): v
                    for k, v in row.items()
                }
                record = {
                    "title": norm.get("short_desc") or norm.get("title") or "",
                    "body": norm.get("long_desc") or norm.get("body") or "",
                    "url": norm.get("url")
                    or norm.get("bug_url")
                    or norm.get("reported_to")
                    or "",
                    "symptom": norm.get("symptom") or norm.get("status") or "",
                }
                records.append(record)
        return records

    def fetch_from_github(self, state: str = "closed", limit: int = 100) -> List[Dict]:
        if not self.github_repo:
            return []
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        records: List[Dict] = []
        url = f"https://api.github.com/repos/{self.github_repo}/issues"
        params: Dict = {"state": state, "labels": "bug", "per_page": min(limit, 100)}

        while url and len(records) < limit:
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
            except Exception:
                break
            for issue in resp.json():
                if "pull_request" in issue:
                    continue
                records.append(
                    {
                        "title": issue.get("title", ""),
                        "body": issue.get("body") or "",
                        "url": issue.get("html_url", ""),
                        "symptom": "",
                    }
                )
            # Follow pagination
            link_header = resp.headers.get("Link", "")
            url = _parse_next_link(link_header)
            params = {}  # next URL already contains params

        return records[:limit]

    def merge_and_deduplicate(self) -> List[Dict]:
        seen_urls: set = set()
        merged: List[Dict] = []
        for record in self.load_from_csv() + self.fetch_from_github():
            url = record.get("url", "")
            key = url if url else record.get("title", "")
            if key and key not in seen_urls:
                seen_urls.add(key)
                merged.append(record)
        return merged


def _parse_next_link(link_header: str) -> Optional[str]:
    """Extract the 'next' URL from a GitHub Link header."""
    for part in link_header.split(","):
        parts = part.strip().split(";")
        if len(parts) == 2 and 'rel="next"' in parts[1]:
            return parts[0].strip().strip("<>")
    return None
