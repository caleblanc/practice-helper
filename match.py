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


def norm(text: str, strip_noise: bool = True) -> str:
    """Comparable form of a title or artist name."""
    if not text:
        return ""
    t = text.lower()
    if strip_noise:
        t = _NOISE.sub("", t)
    t = t.replace("&", " and ")
    t = re.sub(r"[‘’“”']", "", t)   # smart quotes, apostrophes
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\b(the|a|an)\b", " ", t)
    return " ".join(t.split())


def _artist_ok(a: str, b: str) -> bool:
    """Do these two artist names refer to the same act?

    Compared as whole words, never as raw text: "Pol" and "Polar" are both
    substrings of "Polaris" and are both real bands, so substring matching
    hands their tabs to Polaris songs and ranks them above the genuine ones.
    Word containment still allows the usual variations -- "Architects" against
    "Architects UK", "Blue Encount" against "BLUE ENCOUNT".
    """
    at, bt = set(norm(a).split()), set(norm(b).split())
    if not at or not bt:
        return True                    # nothing to contradict
    return at == bt or at <= bt or bt <= at


UNCERTAIN_PENALTY = 0.45     # title matches, artist does not


def rate(track: dict, tab: dict):
    """Return (score, certain). 0 means no match at all.

    A tab whose title matches but whose artist does not is still worth
    offering: Songsterr uploads are frequently misattributed -- Spiritbox's
    "Holy Roller" is filed under "Box Of Spirits" -- so rejecting on artist
    alone loses the correct tab. It is returned as *uncertain* instead, which
    means it is shown for the user to confirm rather than chosen for them.
    """
    artist_ok = _artist_ok(track.get("artistName", ""), tab.get("artist", ""))
    title_score = _title_score(track, tab, allow_short=artist_ok)
    if title_score <= 0:
        return 0.0, False
    # Noise-stripping is right for a streaming title -- "(Remastered 2011)" is
    # not part of the song -- but on a tab title the brackets carry the version:
    # "Holy Roller (Zev Rose live playthrough drums only)" strips down to an
    # exact match and would otherwise beat the plain transcription. Charge for
    # whatever the tab title says that the track title does not.
    extra = max(0, len(norm(tab.get("title", ""), strip_noise=False).split())
                   - len(norm(track.get("trackName", ""), strip_noise=False).split()))
    title_score = max(0.3, title_score - 0.04 * extra)
    if artist_ok:
        return title_score, True
    # Only an exact title is a strong enough signal to survive a wrong artist;
    # a partial title plus a wrong artist is just a different song.
    if title_score < 1.0:
        return 0.0, False
    return title_score * UNCERTAIN_PENALTY, False


def score(track: dict, tab: dict) -> float:
    """Match strength alone, ignoring whether it was certain."""
    return rate(track, tab)[0]


def _title_score(track: dict, tab: dict, allow_short: bool = False) -> float:
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
    # A single-word title is too weak an anchor to extend on its own -- "Blind"
    # would swallow "Blind Faith" -- but when the artist already agrees it is
    # safe, and required: "Masochist" has to reach "Masochist (7 strings)".
    if len(short) < 2 and not allow_short:
        return 0.0
    extra = len(long_) - len(short)
    return max(0.5, 0.9 - 0.05 * extra)


def _annotate(tr, tabs, limit):
    """Best few tabs for one track, each tagged with its score and certainty.

    Ties break toward the plainest title. Normalisation strips "(live …)" and
    similar, which is right for a streaming title but means a tab called
    "Holy Roller (Zev Rose live playthrough drums only)" normalises to an exact
    match and would otherwise outrank the plain transcription.
    """
    certain, uncertain = [], []
    for tb in tabs:
        sc, is_certain = rate(tr, tb)
        if sc <= 0:
            continue
        # Copy: a pool entry can match several tracks, each needing its own
        # verdict.
        row = (sc, len(tb.get("title") or ""), dict(tb, _score=sc, _certain=is_certain))
        (certain if is_certain else uncertain).append(row)

    # A wrong-artist match is a fallback, never an addition. "Polaris" is a
    # song by Jimmy Eat World, a song by Blue Encount and a band with other
    # songs entirely; offering all of them against each other buries the one
    # correct tab. They are only worth showing when nothing by the right artist
    # turned up at all -- which is the case this was built for, a tab filed
    # under the wrong name.
    hits = certain
    if not hits and uncertain:
        # A wrong-artist match is only credible when the title identifies the
        # song on its own. A one-word title like "Polaris" is a song by Jimmy
        # Eat World, a song by Blue Encount and a band with other songs
        # entirely, so same-titled tabs say nothing about which is right. Two
        # words or more is specific enough to be worth offering.
        if len(norm(tr.get("trackName", "")).split()) >= 2:
            hits = uncertain
    hits.sort(key=lambda x: (-x[0], x[1]))
    return [tb for _s, _n, tb in hits[:limit]]


def bucket(tracks: list[dict], tabs: list[dict], limit: int = 3) -> dict[int, list[dict]]:
    """Assign tabs from a shared pool to the tracks they match."""
    out: dict[int, list[dict]] = {}
    for i, tr in enumerate(tracks):
        hits = _annotate(tr, tabs, limit)
        if hits:
            out[i] = hits
    return out


def fill(tracks: list[dict], found: dict[int, list[dict]], search_fn,
         limit: int = 3, workers: int = 6, budget: int = 20,
         on_found=None) -> dict[int, list[dict]]:
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
            tabs = _annotate(tr, res, limit)
            if tabs:
                with lock:
                    found[i] = tabs
                if on_found:
                    # Publish as each lookup lands so the list fills in rather
                    # than appearing all at once at the end.
                    on_found(i, tabs)

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return found
