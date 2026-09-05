"""Songsterr API client."""

import math
import requests

BASE = "https://www.songsterr.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# CDN domains (discovered from JS bundle)
_CDN_STAGE = "d3d3l6a6rcgkaf"
_CDN_PROD = ["dqsljvtekg760", "d34shlm8p2ums2", "d3cqchs6g3b5ew"]
_CDN_NOIMAGE = ["d3rrfvx08uyjp1", "dodkcbujl0ebx", "dj1usja78sinh"]


def search(pattern: str, size: int = 10) -> list[dict]:
    r = requests.get(
        f"{BASE}/api/search",
        params={"pattern": pattern, "size": size},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("records", [])


def get_revisions(song_id: int) -> list[dict]:
    r = requests.get(
        f"{BASE}/api/meta/{song_id}/revisions",
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_revision(revision_id: int) -> dict:
    r = requests.get(
        f"{BASE}/api/revision/{revision_id}",
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_track_data(song_id: int, revision_id: int, image: str, part_index: int, attempt: int = 0) -> dict:
    if image and image.endswith("-stage"):
        url = f"https://{_CDN_STAGE}.cloudfront.net/{song_id}/{revision_id}/{image}/{part_index}.json"
    elif image:
        cdn = _CDN_PROD[attempt % len(_CDN_PROD)]
        url = f"https://{cdn}.cloudfront.net/{song_id}/{revision_id}/{image}/{part_index}.json"
    else:
        cdn = _CDN_NOIMAGE[attempt % len(_CDN_NOIMAGE)]
        url = f"https://{cdn}.cloudfront.net/part/{revision_id}/{part_index}"

    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_song_id_from_url(url: str) -> int | None:
    """Extract song ID from a Songsterr URL like .../tabs-s12345 or .../tab-s12345t1"""
    import re
    m = re.search(r"-s(\d+)(?:t\d+)?(?:\s|$|/)", url)
    if m:
        return int(m.group(1))
    return None
