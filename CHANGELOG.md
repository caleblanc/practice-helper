# Changelog

## 0.02

Fixes to make a fresh install actually work, plus double-click installers.

### Installers

- `install.command` (macOS) and `install.bat` (Windows). Both find a suitable
  Python, fetch the app if run on their own, build an isolated environment,
  and install a real app with an icon — Applications on macOS, Desktop and
  Start Menu on Windows.
- Bootstrap installers rather than frozen bundles: PyTorch is several
  gigabytes and unreliable to freeze, so the environment is built locally.

### Fixes

- **The install instructions were wrong and unrunnable.** The website showed a
  single copyable block that used `python -m venv` (there is no `python` on
  macOS, only `python3`) and put the macOS and Windows activation lines in the
  same block, so pasting it ran both. The venv was never created and pip then
  failed with `externally-managed-environment`. Split per platform, corrected,
  and the styled `$` prompts and `#` comments are gone.
- **CustomTkinter 6.0 compatibility.** A fresh install gets 6.0, which renamed
  the private hit-test the trackpad-scrolling fix depended on, silently
  disabling it. The fix now works on 5.x and 6.x, and falls back to walking the
  widget tree itself rather than depending on a private name at all.

### Changed

- The Songsterr panel header names what it will actually produce:
  "Guitar Pro", "MIDI", or "Guitar Pro & MIDI".

## 0.01 — first release

First tagged release. Everything below is either new in this version or was
reworked for it.

### Streaming services

- Pluggable providers instead of a single hard-wired service: **Apple Music**,
  **Spotify**, **TIDAL**, **Deezer**, and **Local Files**.
- The panel takes the selected service's brand colour — Apple red, Spotify
  green, TIDAL cyan, Deezer purple — across the header, options column,
  Search and Process buttons.
- Search and audio acquisition are separate concerns. Every provider can
  search; acquisition is a command you configure, because no single tool
  covers every service. Providers that cannot download say so plainly.
- First-run setup: no credentials ship with the app, and a welcome dialog
  points at Settings. Songsterr and Guitar Pro export need no account at all.

### Scores

- Songsterr tabs convert straight to Guitar Pro 8 rather than going through
  MIDI, which was quantising everything to a 16th grid and losing notes.
- Articulations carried through: palm mutes, ties, hammer-ons/pull-offs,
  slides, harmonics, bends, tremolo, ghost notes, and accents.
- Correct instrument tuning read from the Songsterr revision.
- Drum articulations mapped through the Guitar Pro kit, including a three-tom
  ladder and separate left/right kick notes where the source distinguishes
  them.

### Audio

- Optional backing track embedded into the `.gp`, aligned to the score by
  cross-correlating drum onsets against the drum stem.
- Stem selection per source, plus a combined mixdown.
- Choice of 4-source or 6-source demucs model.

### Fixes

- Library lookup ignored punctuation on one side only, so a title containing
  an apostrophe was never found and stems silently never extracted.
- The same lookup matched substrings, so "Bleed" could resolve to
  "Bleeding Mascara" and embed the wrong song's audio.
- Stem output folder was hardcoded to one model name, so the 6-source model
  produced nothing.
- Trackpad scrolling did nothing on Tk 9, which routes precision scrolling to
  `<TouchpadScroll>` rather than `<MouseWheel>`.

### Cross-platform

- No hardcoded paths. Helper tools are discovered on `PATH`, in the running
  venv, or in a folder you set.
- Config lives beside the app when that is writable, and in the per-user
  config directory otherwise.
- Runs on macOS and Windows.
