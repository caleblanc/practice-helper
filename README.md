# Practice Helper

**v0.01**

Find a song, pull down its audio, split it into stems, and get a Guitar Pro
score — from one window.

Built for learning parts by ear and by eye at the same time: the score comes
from Songsterr, the audio comes from your streaming service, and the two can be
combined into a single Guitar Pro file with the recording embedded and lined up
to the notation.

---

## What it does

| | |
|---|---|
| **Songsterr → Guitar Pro 8** | Full arrangement, written directly as `.gp` — no MIDI round-trip, so nothing is quantised away |
| **Songsterr → MIDI** | Standard MIDI file, filtered by instrument |
| **Streaming → stems** | Split a track into drums / bass / vocals / other (or a 6-source split adding guitar and piano) |
| **Audio in the score** | Embed the recording into the `.gp`, aligned to bar 1 by matching drum onsets |
| **Organised output** | Every song lands in its own `Artist - Title/` folder |

Songsterr and Guitar Pro export need **no account**. A streaming service is
only required for audio and stems.

## Supported services

| Service | Search | Download |
|---|---|---|
| Apple Music | ✅ | ✅ via [gamdl](https://github.com/glomatico/gamdl) and your own subscription cookies |
| Spotify | ✅ your own API app | ❌ no download API exists |
| TIDAL | ✅ your own API app | ❌ supply your own command |
| Deezer | ✅ no account needed | ❌ supply your own command |
| Local Files | ✅ folder search | n/a — already on disk |

Search and downloading are deliberately separate. Any service can be used to
*find* a track; getting the audio is a command you configure in Settings, so
the app never has to bundle a downloader for a service that has no legitimate
one. Providers that cannot download still work fine — pair them with a local
copy of the audio.

The interface takes on the brand colour of whichever service is selected.

## Requirements

- Python 3.11+
- Guitar Pro 8 (to open the scores)
- **ffmpeg** on your `PATH` — required on Windows and Linux, optional on macOS
- Optional: `gamdl` for Apple Music downloads, and an Apple Music subscription

## Install

```bash
git clone https://github.com/caleblanc/practice-helper
cd practice-helper
python3 -m venv .venv
```

macOS / Linux:

```bash
source .venv/bin/activate && pip install -r requirements.txt
```

Windows:

```bat
.venv\Scripts\activate && pip install -r requirements.txt
```

## Run

```bash
./launch.command      # macOS / Linux
```

```bat
launch.bat
```

On first launch the app has nothing configured and will offer to open Settings.
Everything is set up from there — no config file editing required. If you would
rather start from a file, copy `config.example.json` to `config.json`.

## First-time setup per service

**Apple Music** — export your `music.apple.com` cookies to a `cookies.txt`
(any "Get cookies.txt" browser extension), point Settings at it, and make sure
`gamdl` is installed.

**Spotify** — create a free app at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and
paste the Client ID and Secret into Settings.

**TIDAL** — create an app at
[developer.tidal.com](https://developer.tidal.com), then enter the Client ID,
Secret, and your country code.

**Deezer** — nothing to set up.

**Local Files** — point Settings at your music folder.

Credentials are stored in your local `config.json`, which is git-ignored and
never leaves your machine.

## Where things go

```
<songs folder>/
└── Artist - Title/
    ├── Artist - Title.gp          score, optionally with audio embedded
    ├── Artist - Title.mid
    ├── Artist - Title (Full).m4a
    ├── Artist - Title (OD).wav    drums only
    └── Artist - Title (bass).wav  …and any other stems you ticked
```

## Notes

Stem separation is [demucs](https://github.com/adefossez/demucs). It is slow on
CPU and much faster on an Apple Silicon GPU or CUDA, both of which are used
automatically when available.

Audio alignment assumes a roughly constant tempo. It finds the offset by
cross-correlating the score's drum hits against the extracted drum stem, and
reports a confidence score — a low one usually means the recording drifts and
the single offset will not hold all the way through.

See [DISCLAIMER.md](DISCLAIMER.md) before use.

## Licence

MIT — see [LICENSE](LICENSE).
