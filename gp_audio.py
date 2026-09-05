#!/usr/bin/env python3
"""
Embed a backing track into a Guitar Pro 8 file and line it up with the score.

Guitar Pro stores audio as a top-level <BackingTrack> node plus an <Assets>
entry pointing at a file inside the archive. Alignment is a single
<FramePadding> value measured in audio frames (padding / 44100 = seconds).

Offset is derived by comparing the first drum hit in the isolated drum stem
against the first drum note in the score, so only that one landmark matters.
"""

import os, re, json, shutil, zipfile, hashlib, tempfile, wave
from fractions import Fraction as F
import xml.etree.ElementTree as ET

NOTE_VALUE = {"Whole":1,"Half":2,"Quarter":4,"Eighth":8,"16th":16,"32nd":32,"64th":64}
SAMPLE_RATE = 44100


# ── audio helpers ────────────────────────────────────────────────────────────

def _read_wav(path):
    import numpy as np
    with wave.open(str(path)) as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        raw = np.frombuffer(w.readframes(n), dtype="<i2").astype("float32") / 32768.0
    return raw.reshape(-1, ch), sr


def mix_stems(paths, dest):
    """Sum several stem wavs into one file. Returns dest, or None if none exist."""
    import numpy as np, soundfile as sf
    mix, rate = None, None
    for p in paths:
        if not os.path.exists(p):
            continue
        data, rate = sf.read(str(p), always_2d=True)
        mix = data if mix is None else mix[:len(data)] + data[:len(mix)]
    if mix is None:
        return None
    sf.write(str(dest), np.clip(mix, -1.0, 1.0), rate)
    return dest


def first_onset(wav_path):
    """Time in seconds of the first drum hit in an isolated drum stem.

    Broadband on purpose: the score's first drum note may be a snare or cymbal,
    so band-limiting to the kick would land on the wrong hit.
    """
    import numpy as np
    from scipy.ndimage import maximum_filter1d, uniform_filter1d
    x, sr = _read_wav(wav_path)
    x = x.mean(axis=1)
    H, N = 512, 2048
    win = np.hanning(N).astype("float32")
    frames = 1 + (len(x) - N) // H
    if frames < 4:
        return None
    S = np.empty((frames, N // 2 + 1), dtype="float32")
    for i in range(frames):
        S[i] = np.abs(np.fft.rfft(x[i * H:i * H + N] * win))
    flux = np.maximum(0, np.diff(S, axis=0)).sum(axis=1)
    if flux.max() <= 0:
        return None
    flux /= flux.max()
    t = (np.arange(len(flux)) * H + N / 2) / sr
    loc = maximum_filter1d(flux, 7)
    thr = uniform_filter1d(flux, int(0.5 * sr / H)) * 1.6 + 0.02
    hits = np.where((flux == loc) & (flux > thr))[0]
    return float(t[hits[0]]) if len(hits) else None


# ── score helpers ────────────────────────────────────────────────────────────

def _beat_duration(rhythm):
    d = F(1, NOTE_VALUE[rhythm.findtext("NoteValue")])
    dot = rhythm.find("AugmentationDot")
    if dot is not None:
        d *= (2 - F(1, 2 ** int(dot.get("count"))))
    tup = rhythm.find("PrimaryTuplet")
    if tup is not None:
        d *= F(int(tup.get("den")), int(tup.get("num")))
    return d


def score_onsets(gp_path, limit=None):
    """Every drum-note onset in the score, in seconds from bar 1."""
    return _walk_drums(gp_path, collect_all=True, limit=limit)


def first_note_time(gp_path):
    """Seconds from bar 1 to the first sounding note of the drum track."""
    hits = _walk_drums(gp_path, collect_all=False)
    return hits[0] if hits else None


def _walk_drums(gp_path, collect_all, limit=None):
    out = []
    with zipfile.ZipFile(str(gp_path)) as z:
        root = ET.fromstring(z.read("Content/score.gpif").decode("utf-8"))
    tracks = list(root.find("Tracks"))
    drum = 0
    for i, t in enumerate(tracks):
        iset = t.find("InstrumentSet")
        if iset is not None and iset.findtext("Type") == "drumKit":
            drum = i; break

    rh = {r.get("id"): r for r in root.find("Rhythms")}
    bt = {b.get("id"): b for b in root.find("Beats")}
    vo = {v.get("id"): v for v in root.find("Voices")}
    bars = list(root.find("Bars"))

    tempos = []
    for a in root.iter("Automation"):
        if a.findtext("Type") == "Tempo":
            tempos.append((int(a.findtext("Bar")), float(a.findtext("Position") or 0),
                           float(a.findtext("Value").split()[0])))
    tempos.sort()
    bpm0 = tempos[0][2] if tempos else 120.0

    def bpm_at(bar):
        cur = bpm0
        for b, _p, v in tempos:
            if b <= bar: cur = v
            else: break
        return cur

    seconds = 0.0
    for mi, mb in enumerate(root.find("MasterBars")):
        num, den = (int(x) for x in mb.findtext("Time").split("/"))
        bar_len = F(num, den)
        whole_secs = 4 * 60.0 / bpm_at(mi)          # seconds per whole note
        bar_ids = mb.findtext("Bars").split()
        if drum < len(bar_ids) and bar_ids[drum] != "-1":
            bar = bars[int(bar_ids[drum])]
            for vid in bar.findtext("Voices").split():
                if vid == "-1":
                    continue
                pos = F(0)
                for bid in vo[vid].findtext("Beats").split():
                    beat = bt[bid]
                    if beat.findtext("Notes"):
                        t = seconds + float(pos) * whole_secs
                        if not collect_all:
                            return [t]
                        out.append(t)
                    pos += _beat_duration(rh[beat.find("Rhythm").get("ref")])
        seconds += float(bar_len) * whole_secs
        if limit and seconds > limit:
            break
    return sorted(out)


# ── embedding ────────────────────────────────────────────────────────────────

BACKING = """<BackingTrack>
<IconId>21</IconId>
<Color>0 0 0</Color>
<Name><![CDATA[Audio Track]]></Name>
<ShortName><![CDATA[a.track]]></ShortName>
<PlaybackState>Default</PlaybackState>
<Enabled>true</Enabled>
<Source>Local</Source>
<AssetId>0</AssetId>
<ChannelStrip>
<Parameters>0.500000 0.500000 0.500000 0.500000 0.500000 0.500000 0.500000 0.500000 0.500000 0.000000 0.500000 0.500000 0.800000 0.500000 0.500000 0.500000</Parameters>
<YouTubeVideoUrl></YouTubeVideoUrl>
<Filter>6</Filter>
<FramesPerPixel>400</FramesPerPixel>
</ChannelStrip>
<FramePadding>%d</FramePadding>
<Semitones>0</Semitones>
<Cents>0</Cents>
</BackingTrack>
"""

ASSETS = """<Assets>
<Asset id="0">
<OriginalFilePath><![CDATA[%s]]></OriginalFilePath>
<OriginalFileSha1><![CDATA[%s]]></OriginalFileSha1>
<EmbeddedFilePath><![CDATA[Content/Assets/%s.wav]]></EmbeddedFilePath>
</Asset>
</Assets>
"""


def embed(gp_path, audio_path, out_path, padding_frames=0, original_path=None):
    """Write a copy of *gp_path* carrying *audio_path* as an aligned backing track."""
    gp_path, audio_path, out_path = str(gp_path), str(audio_path), str(out_path)
    sha = hashlib.sha1(open(audio_path, "rb").read(1 << 20)).hexdigest()[:32]
    padding_frames = max(0, int(padding_frames))

    with zipfile.ZipFile(gp_path) as z:
        members = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
    gpif = members["Content/score.gpif"].decode("utf-8")

    gpif = re.sub(r"<BackingTrack>.*?</BackingTrack>", "", gpif, flags=re.S)
    gpif = re.sub(r"<Assets>.*?</Assets>", "", gpif, flags=re.S)
    gpif = gpif.replace("</MasterTrack>", "</MasterTrack>\n" + BACKING % padding_frames, 1)
    gpif = gpif.replace("</Rhythms>", "</Rhythms>\n" +
                        ASSETS % (original_path or audio_path, sha, sha), 1)
    members["Content/score.gpif"] = gpif.encode("utf-8")
    members["Content/Assets/%s.wav" % sha] = open(audio_path, "rb").read()
    members["meta.json"] = (json.dumps({"hasAudio": True, "version": "1.0.0"},
                                       indent=4) + "\n").encode("utf-8")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for d in ("Content/", "Content/Assets/", "Content/ScoreViews/", "Content/Stylesheets/"):
            zi = zipfile.ZipInfo(d); zi.external_attr = 0o40755 << 16; z.writestr(zi, b"")
        for n in sorted(members):
            z.writestr(n, members[n])
    return out_path


def estimate_offset(gp_path, drum_stem, max_lag=40.0, window=180.0):
    """Offset in seconds between score and recording, by correlating all hits.

    A single landmark is brittle -- if the first notated hit is not the first
    audible one, the result is out by seconds. Correlating the whole onset
    pattern instead lets every hit vote, and the peak sharpness doubles as a
    confidence measure.
    """
    import numpy as np
    from scipy.ndimage import uniform_filter1d
    onsets = score_onsets(gp_path, limit=window)
    if not onsets:
        return None, 0.0
    x, sr = _read_wav(drum_stem)
    x = x.mean(axis=1)[: int(window * sr)]
    H, N = 512, 2048
    fps = sr / H
    win = np.hanning(N).astype("float32")
    frames = 1 + (len(x) - N) // H
    if frames < 16:
        return None, 0.0
    S = np.empty((frames, N // 2 + 1), dtype="float32")
    for i in range(frames):
        S[i] = np.abs(np.fft.rfft(x[i * H:i * H + N] * win))
    flux = np.maximum(0, np.diff(S, axis=0)).sum(axis=1)
    if flux.max() <= 0:
        return None, 0.0
    flux = flux / flux.max()
    flux -= uniform_filter1d(flux, int(fps))          # de-trend

    spikes = np.zeros(len(flux), dtype="float32")
    for t in onsets:
        i = int(round(t * fps))
        if 0 <= i < len(spikes):
            spikes[i] = 1.0
    if spikes.sum() < 4:
        return None, 0.0
    spikes = uniform_filter1d(spikes, 3)
    spikes -= spikes.mean()

    lags = int(max_lag * fps)
    corr = np.correlate(flux, spikes, mode="full")
    mid = len(flux) - 1
    seg = corr[max(0, mid - lags): mid + lags + 1]
    if not len(seg):
        return None, 0.0
    best = int(np.argmax(seg))
    peak = seg[best]
    offset = (best - min(lags, mid)) / fps
    med = float(np.median(np.abs(seg)))
    confidence = float(peak / med) if med > 0 else 0.0
    return offset, confidence


def align_and_embed(gp_path, drum_stem, audio_path, out_path, log=print):
    """Compute the offset from the drum stem and embed *audio_path*."""
    offset, conf = (None, 0.0)
    if drum_stem and os.path.exists(drum_stem):
        offset, conf = estimate_offset(gp_path, drum_stem)
    if offset is None:
        log("  ⚠ could not align (missing drum stem or empty score) — embedding at 0")
        offset = 0.0
    else:
        log("  aligned by correlating %d drum hits -> offset %+.0f ms (confidence %.1fx)"
            % (len(score_onsets(gp_path, limit=180.0)), offset * 1000, conf))
        if conf < 3.0:
            log("  ⚠ weak alignment match — check the audio lines up")
        if offset < 0:
            log("  ⚠ audio starts before the score; clamping to 0")
    return embed(gp_path, audio_path, out_path,
                 padding_frames=round(max(0.0, offset) * SAMPLE_RATE))
