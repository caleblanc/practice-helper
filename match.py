#!/usr/bin/env python3
"""Pairing streaming tracks with Songsterr tabs.

Titles from the two sources rarely agree character for character: streaming
catalogues carry remaster tags, feature credits and album-version suffixes that
tabs never have. So compare a normalised form, and treat artist agreement as a
requirement rather than a bonus — "Blind" by Korn and "Blind" by Lifehouse are
otherwise indistinguishable.
"""

from __future__ import annotations

import re
import threading

# Trailing noise that streaming services add and tab sites do not.
_NOISE = re.compile(
    r"""\s*(?:
          [\(\[][^)\]]*(?:remaster|remix|live|version|edit|mix|mono|stereo|
                          deluxe|bonus|explicit|clean|demo|acoustic|radio|
                          single|album|anniversary|reissue|feat|ft)\b[^)\]]*[\)\]]
        | -\s*(?:remaster|remastered|live|single|radio|album)\b.*$
        | \s*(?:feat|ft)\.?\s+.*$
        )""",
    re.I | re.X,
)


def norm(text: str) -> str:
    """Comparable form of a title or artist name."""
    if not text:
        return ""
    t = text.lower()
    t = _NOISE.sub("", t)
    t = t.replace("&", " and ")
    t = re.sub(r"[‘’“”']", "", t)   # smart quotes, apostrophes
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\b(the|a|an)\b", " ", t)
    return " ".join(t.split())


def _artist_ok(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return True                    # nothing to contradict
    return a == b or a in b or b in a


def score(track: dict, tab: dict) -> float:
    """0 = no match. Higher is better."""
    if not _artist_ok(track.get("artistName", ""), tab.get("artist", "")):
        return 0.0
    t, s = norm(track.get("trackName", "")).split(), norm(tab.get("title", "")).split()
    if not t or not s:
        return 0.0
    if t == s:
        return 1.0

    # Compare whole words, never raw substrings: "Twist" is not a match for
    # "Twisted Transistor", though one is a prefix of the other as text.
    short, long_ = (t, s) if len(t) <= len(s) else (s, t)
    if long_[:len(short)] != short:
        return 0.0
    # A single-word title is too weak an anchor to extend -- "Blind" would
    # happily swallow "Blind Faith" -- so only extend from two words up.
    if len(short) < 2:
        return 0.0
    extra = len(long_) - len(short)
    return max(0.5, 0.9 - 0.05 * extra)


def bucket(tracks: list[dict], tabs: list[dict], limit: int = 3) -> dict[int, list[dict]]:
    """Assign tabs from a shared pool to the tracks they match."""
    out: dict[int, list[dict]] = {}
    for i, tr in enumerate(tracks):
        scored = [(score(tr, tb), tb) for tb in tabs]
        hits = sorted([x for x in scored if x[0] > 0], key=lambda x: -x[0])
        if hits:
            out[i] = [tb for _s, tb in hits[:limit]]
    return out


def fill(tracks: list[dict], found: dict[int, list[dict]], search_fn,
         limit: int = 3, workers: int = 6, budget: int = 20) -> dict[int, list[dict]]:
    """Look up tabs individually for tracks the shared pool missed.

    One broad search cannot cover a whole discography, so anything still
    unmatched gets its own query. Bounded in both concurrency and total number
    of requests so a 200-result search cannot turn into a stampede.
    """
    todo = [i for i in range(len(tracks)) if not found.get(i)][:budget]
    if not todo:
        return found
    lock = threading.Lock()
    sem = threading.Semaphore(workers)

    def work(i):
        with sem:
            tr = tracks[i]
            q = f"{tr.get('artistName','')} {tr.get('trackName','')}".strip()
            try:
                res = search_fn(q, 10)
            except Exception:
                return
            hits = sorted([(score(tr, tb), tb) for tb in res if score(tr, tb) > 0],
                          key=lambda x: -x[0])
            if hits:
                with lock:
                    found[i] = [tb for _s, tb in hits[:limit]]

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return found
