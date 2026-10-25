"""Trader-source discovery and conservative, human-reviewed BTC long extraction.

Official YouTube API generally will not provide captions for third-party uploads.
This module only imports transcripts the user is authorised to provide.
"""
from __future__ import annotations
import hashlib
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def discover_videos(db, channel_id: str) -> list[dict]:
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ValueError("Provide a standard YouTube channel ID beginning UC")
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    request = urllib.request.Request(url, headers={"User-Agent": "btc-edge-research/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())
    discovered = datetime.now(timezone.utc).isoformat()
    videos = []
    for entry in root.findall(f"{ATOM}entry"):
        id_node = entry.find(f"{YT}videoId")
        title_node = entry.find(f"{ATOM}title")
        published_node = entry.find(f"{ATOM}published")
        if id_node is None or title_node is None or published_node is None:
            continue
        video_id = (id_node.text or "").strip()
        title = title_node.text or ""
        published = published_node.text or ""
        video = {"video_id": video_id, "channel_id": channel_id, "title": title,
                 "published_at": published, "url": f"https://www.youtube.com/watch?v={video_id}"}
        db.execute("""INSERT OR IGNORE INTO videos(video_id,channel_id,title,published_at,url,discovered_at)
                      VALUES(?,?,?,?,?,?)""", (video_id, channel_id, title, published,
                                               video["url"], discovered))
        videos.append(video)
    db.commit()
    return videos


# Strict template only. Unstructured commentary and hypotheticals must be reviewed manually.
PATTERN = re.compile(
    r"\bBTC\s+LONG\b[^\n]*?\bentry\s*[:=]\s*\$?([\d,]+(?:\.\d+)?)"
    r"[^\n]*?\bstop\s*[:=]\s*\$?([\d,]+(?:\.\d+)?)"
    r"[^\n]*?\btarget\s*[:=]\s*\$?([\d,]+(?:\.\d+)?)",
    flags=re.IGNORECASE,
)


def extract_review_candidate(text: str, source_url: str, published_at: str,
                             observed_at: str | None = None) -> dict:
    """Intentionally fail closed, never invent missing entry/stop/target levels."""
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if published.tzinfo is None or observed.tzinfo is None:
        raise ValueError("Timestamps must include timezone")
    if observed < published:
        raise ValueError("Observation cannot precede publication")
    if not source_url.startswith("https://"):
        raise ValueError("Expected a verifiable https source URL")
    matches = list(PATTERN.finditer(text))
    if len(matches) != 1:
        raise ValueError("Exactly one explicit 'BTC LONG entry: ... stop: ... target: ...' statement required")
    m = matches[0]
    entry, stop, target = [float(s.replace(",", "")) for s in m.groups()]
    if not (0 < stop < entry < target):
        raise ValueError("Invalid BTC long entry, stop or target")
    excerpt = m.group(0).strip()[:1000]
    identifier = hashlib.sha256(f"{source_url}|{published_at}|{excerpt}".encode()).hexdigest()[:20]
    age_minutes = (observed - published).total_seconds() / 60
    return {"signal_id": identifier, "source_url": source_url,
            "source_published_at": published.isoformat(), "observed_at": observed.isoformat(),
            "source_excerpt": excerpt, "entry_price": entry, "stop_price": stop,
            "target_price": target, "status": "REVIEW_REQUIRED",
            "notes": f"Captured {age_minutes:.1f} minutes after publication; NOT an executable instruction."}


def save_candidate(db, signal: dict) -> None:
    keys = ("signal_id", "source_url", "source_published_at", "observed_at",
            "source_excerpt", "entry_price", "stop_price", "target_price", "status", "notes")
    db.execute(f"INSERT OR IGNORE INTO external_signals({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
               tuple(signal[k] for k in keys))
    db.commit()
