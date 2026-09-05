#!/usr/bin/env python3
"""Streaming-service providers.

Two independent halves, deliberately kept apart:

  * **Search** — turning a query into track metadata. Every provider implements
    this, and it is the half that decides what the UI shows.
  * **Acquisition** — getting the actual audio file. This is a per-provider
    external command the user configures, because no single tool covers every
    service and the app has no business bundling one.

A provider that can search but not acquire is still useful: pick the track
here, and point the app at a local copy of the audio.

Every search returns the same normalised dict so the UI never branches on
provider:

    {"trackName", "artistName", "collectionName", "trackViewUrl", "provider"}
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

TIMEOUT = 12


# ── Credential descriptors ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Field:
    """One thing the user has to supply before a provider works."""
    key: str                      # config key, namespaced per provider
    label: str                    # shown in the setup dialog
    kind: str = "text"            # text | secret | file | dir
    help: str = ""


@dataclass
class Provider:
    id: str
    name: str
    accent: str                   # brand colour, used for headers and buttons
    dim: str                      # darker shade, for button hover states
    tint: str                     # very dark shade, for the options column
    fields: tuple = ()
    # Which of `fields` searching actually needs. Apple Music's cookies, for
    # example, authorise downloads -- its search API is public -- so demanding
    # them before a search blocks something that works perfectly well without.
    # None means "all of them".
    search_fields: tuple | None = None
    # {url}, {out} and any credential key are substituted at run time.
    download_cmd: str = ""
    can_download: bool = False    # False => search only, bring your own audio
    note: str = ""

    def search(self, query: str, creds: dict, limit: int = 20) -> list[dict]:
        raise NotImplementedError

    def missing(self, creds: dict) -> list[Field]:
        """Credential fields that are still blank."""
        return [f for f in self.fields if not (creds.get(f.key) or "").strip()]

    def missing_for_search(self, creds: dict) -> list[Field]:
        """Only the fields searching cannot proceed without."""
        needed = self.fields if self.search_fields is None else self.search_fields
        return [f for f in needed if not (creds.get(f.key) or "").strip()]


def _norm(provider: str, title, artist, album, url) -> dict:
    return {"trackName": title or "Unknown", "artistName": artist or "",
            "collectionName": album or "", "trackViewUrl": url or "",
            "provider": provider}


# ── Apple Music ───────────────────────────────────────────────────────────────

class AppleMusic(Provider):
    """iTunes Search, with an artist-catalogue fallback.

    /search is public and needs no account, but it matches terms strictly and
    misses plenty: "polaris masochist" returns nothing at all, though Polaris's
    "Masochist" is in the catalogue and the Apple Music app finds it instantly.
    /lookup by artist id does return it, so when a term search comes up short
    the artist is resolved first and their songs filtered locally.
    """

    def _term_search(self, query, limit):
        r = httpx.get("https://itunes.apple.com/search",
                      params={"term": query, "media": "music",
                              "entity": "song", "limit": limit},
                      timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("results", [])

    def _artist_catalogue(self, query, limit):
        import re as _re
        words = [w for w in _re.split(r"\W+", query.lower()) if w]
        if not words:
            return []
        # The whole query rarely names an artist -- "polaris masochist" matches
        # none -- so fall back to progressively shorter leading phrases, which
        # is how people type: artist first, then song.
        artists = []
        for take in range(len(words), 0, -1):
            phrase = " ".join(words[:take])
            try:
                r = httpx.get("https://itunes.apple.com/search",
                              params={"term": phrase, "media": "music",
                                      "entity": "musicArtist", "limit": 8},
                              timeout=TIMEOUT)
                r.raise_for_status()
                artists = r.json().get("results", [])
            except Exception:
                artists = []
            if artists:
                break
        if not artists:
            return []

        # Several artists share a name -- there are ten "Polaris" -- so all the
        # candidates are fetched at once rather than one after another, which
        # took five seconds.
        import concurrent.futures as _cf

        def catalogue(a):
            aid = a.get("artistId")
            if not aid:
                return []
            name_words = {w for w in _re.split(r"\W+", (a.get("artistName") or "").lower()) if w}
            # Whatever the user typed that is not the artist's name is the song.
            rest = [w for w in words if w not in name_words]
            try:
                lk = httpx.get("https://itunes.apple.com/lookup",
                               params={"id": aid, "entity": "song", "limit": 200},
                               timeout=TIMEOUT)
                lk.raise_for_status()
                songs = [x for x in lk.json().get("results", [])
                         if x.get("wrapperType") == "track"]
            except Exception:
                return []
            keep = []
            for t in songs:
                title = (t.get("trackName") or "").lower()
                if rest and not all(w in title for w in rest):
                    continue
                keep.append(t)
            return keep

        out, seen = [], set()
        cands = artists[:6]
        with _cf.ThreadPoolExecutor(max_workers=6) as ex:
            for songs in ex.map(catalogue, cands):
                for t in songs:
                    key = (t.get("artistName"), t.get("trackName"), t.get("collectionName"))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(t)
                    if len(out) >= limit:
                        return out
        return out

    def search(self, query, creds, limit=20):
        results = self._term_search(query, limit)
        if len(results) < 3:
            have = {(t.get("artistName"), t.get("trackName")) for t in results}
            for t in self._artist_catalogue(query, limit - len(results)):
                if (t.get("artistName"), t.get("trackName")) not in have:
                    results.append(t)
        return [_norm(self.id, t.get("trackName"), t.get("artistName"),
                      t.get("collectionName"), t.get("trackViewUrl"))
                for t in results]


# ── Deezer ────────────────────────────────────────────────────────────────────

class Deezer(Provider):
    def search(self, query, creds, limit=20):
        r = httpx.get("https://api.deezer.com/search",
                      params={"q": query, "limit": limit}, timeout=TIMEOUT)
        r.raise_for_status()
        return [_norm(self.id, t.get("title"),
                      (t.get("artist") or {}).get("name"),
                      (t.get("album") or {}).get("title"), t.get("link"))
                for t in r.json().get("data", [])]


# ── Spotify ───────────────────────────────────────────────────────────────────

class Spotify(Provider):
    """Client-credentials flow: the user registers their own app.

    Spotify has no download endpoint at all, so this provider searches only.
    """
    _tok: tuple[str, float] = ("", 0.0)

    def _token(self, creds):
        tok, exp = Spotify._tok
        if tok and time.time() < exp:
            return tok
        cid = (creds.get("spotify_client_id") or "").strip()
        sec = (creds.get("spotify_client_secret") or "").strip()
        if not cid or not sec:
            raise RuntimeError("Spotify client ID and secret are not set up yet.")
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        r = httpx.post("https://accounts.spotify.com/api/token",
                       data={"grant_type": "client_credentials"},
                       headers={"Authorization": f"Basic {basic}"}, timeout=TIMEOUT)
        if r.status_code == 400:
            raise RuntimeError("Spotify rejected those credentials.")
        r.raise_for_status()
        j = r.json()
        # Refresh a minute early rather than racing the expiry.
        Spotify._tok = (j["access_token"], time.time() + j.get("expires_in", 3600) - 60)
        return Spotify._tok[0]

    def search(self, query, creds, limit=20):
        r = httpx.get("https://api.spotify.com/v1/search",
                      params={"q": query, "type": "track", "limit": limit},
                      headers={"Authorization": f"Bearer {self._token(creds)}"},
                      timeout=TIMEOUT)
        r.raise_for_status()
        out = []
        for t in r.json().get("tracks", {}).get("items", []):
            artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
            out.append(_norm(self.id, t.get("name"), artists,
                             (t.get("album") or {}).get("name"),
                             (t.get("external_urls") or {}).get("spotify")))
        return out


# ── Tidal ─────────────────────────────────────────────────────────────────────

class Tidal(Provider):
    """Tidal's developer API, also client credentials."""
    _tok: tuple[str, float] = ("", 0.0)

    def _token(self, creds):
        tok, exp = Tidal._tok
        if tok and time.time() < exp:
            return tok
        cid = (creds.get("tidal_client_id") or "").strip()
        sec = (creds.get("tidal_client_secret") or "").strip()
        if not cid or not sec:
            raise RuntimeError("Tidal client ID and secret are not set up yet.")
        basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
        r = httpx.post("https://auth.tidal.com/v1/oauth2/token",
                       data={"grant_type": "client_credentials"},
                       headers={"Authorization": f"Basic {basic}"}, timeout=TIMEOUT)
        if r.status_code in (400, 401):
            raise RuntimeError("Tidal rejected those credentials.")
        r.raise_for_status()
        j = r.json()
        Tidal._tok = (j["access_token"], time.time() + j.get("expires_in", 3600) - 60)
        return Tidal._tok[0]

    def search(self, query, creds, limit=20):
        r = httpx.get("https://openapi.tidal.com/v2/searchResults/" + query,
                      params={"countryCode": (creds.get("tidal_country") or "US").upper(),
                              "include": "tracks"},
                      headers={"Authorization": f"Bearer {self._token(creds)}",
                               "accept": "application/vnd.api+json"},
                      timeout=TIMEOUT)
        r.raise_for_status()
        out = []
        for item in (r.json().get("included") or []):
            if item.get("type") != "tracks":
                continue
            a = item.get("attributes") or {}
            out.append(_norm(self.id, a.get("title"),
                             a.get("artistName") or "", a.get("albumTitle") or "",
                             (a.get("externalLinks") or [{}])[0].get("href", "")))
            if len(out) >= limit:
                break
        return out


# ── Local library ─────────────────────────────────────────────────────────────

class LocalLibrary(Provider):
    """Audio you already have on disk. No account, no network, no downloader."""
    EXTS = {".m4a", ".mp3", ".wav", ".flac", ".aiff", ".aif", ".ogg", ".opus"}

    def search(self, query, creds, limit=20):
        root = (creds.get("local_root") or "").strip()
        if not root:
            raise RuntimeError("Set your music folder in Settings first.")
        base = Path(root).expanduser()
        if not base.is_dir():
            raise RuntimeError(f"Music folder not found: {base}")
        needle = query.lower()
        out = []
        for p in base.rglob("*"):
            if p.suffix.lower() not in self.EXTS:
                continue
            if needle not in p.stem.lower() and needle not in p.parent.name.lower():
                continue
            # …/Artist/Album/01 Title.m4a is the near-universal layout.
            album = p.parent.name
            artist = p.parent.parent.name if p.parent.parent != base else ""
            out.append(_norm(self.id, p.stem, artist, album, p.as_uri()))
            if len(out) >= limit:
                break
        return out


# ── Registry ──────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, Provider] = {}


def _add(p: Provider):
    PROVIDERS[p.id] = p
    return p


_add(AppleMusic(
    id="apple", name="Apple Music",
    accent="#fc3c44", dim="#c02b32", tint="#3a1418",
    fields=(Field("apple_cookies", "Cookies file", "file",
                  "Exported from a browser signed in to music.apple.com"),),
    search_fields=(),          # the iTunes Search API is public
    download_cmd='gamdl --cookies-path "{apple_cookies}" --output-path "{out}" '
                 '--no-exceptions "{url}"',
    can_download=True,
    note="Needs an Apple Music subscription and gamdl on your PATH."))

_add(Spotify(
    id="spotify", name="Spotify",
    accent="#1db954", dim="#158a3e", tint="#0c2c18",
    fields=(Field("spotify_client_id", "Client ID", "text",
                  "Create a free app at developer.spotify.com/dashboard"),
            Field("spotify_client_secret", "Client secret", "secret")),
    can_download=False,
    note="Search only — Spotify has no download API. Pair it with a local "
         "copy of the audio, or set your own command below."))

_add(Tidal(
    id="tidal", name="TIDAL",
    accent="#00d5d5", dim="#009a9a", tint="#062a2a",
    fields=(Field("tidal_client_id", "Client ID", "text",
                  "Create an app at developer.tidal.com"),
            Field("tidal_client_secret", "Client secret", "secret"),
            Field("tidal_country", "Country code", "text", "e.g. US, GB, AU")),
    can_download=False,
    note="Search only. Supply your own downloader command if you have one."))

_add(Deezer(
    id="deezer", name="Deezer",
    accent="#a238ff", dim="#7a24c4", tint="#241038",
    can_download=False,
    note="Public search API — no account needed. Search only."))

_add(LocalLibrary(
    id="local", name="Local Files",
    accent="#8e8e93", dim="#636366", tint="#232326",
    fields=(Field("local_root", "Music folder", "dir",
                  "Searched recursively for audio files"),),
    can_download=False,
    note="Audio already on this machine. Nothing is downloaded."))

DEFAULT_PROVIDER = "apple"
ORDER = ["apple", "spotify", "tidal", "deezer", "local"]


def get(pid: str) -> Provider:
    return PROVIDERS.get(pid) or PROVIDERS[DEFAULT_PROVIDER]
