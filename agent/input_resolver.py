from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class DriveResource:
    resource_type: str
    resource_id: str


_PATTERNS = [
    ("spreadsheet", re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)")),
    ("file", re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)")),
    ("folder", re.compile(r"drive\.google\.com/drive/folders/([A-Za-z0-9_-]+)")),
]


def resolve_drive_url(url: str) -> DriveResource:
    value = (url or "").strip()
    if not value:
        raise ValueError("Drive URL is required")

    for resource_type, pattern in _PATTERNS:
        match = pattern.search(value)
        if match:
            return DriveResource(resource_type=resource_type, resource_id=match.group(1))

    parsed = urlparse(value)
    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if query_id and parsed.netloc.endswith("drive.google.com"):
        return DriveResource(resource_type="file", resource_id=query_id)

    raise ValueError(f"Unsupported Google Drive URL: {url}")
