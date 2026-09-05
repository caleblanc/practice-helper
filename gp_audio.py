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

# ── small filters ────────────────────────────────────────────────────────────
# These were scipy.ndimage calls, but scipy is a heavy dependency to carry for
# two one-line filters -- and its absence silently broke every embed, since the
# ImportError surfaced only as "audio embed failed". numpy is already required.

def _moving_avg(x, size):
    """uniform_filter1d: centred moving average, edges handled by reflection."""
    import numpy as np
    size = max(1, int(size))
    if size == 1:
        return x.astype("float32", copy=True)
    pad = size // 2
    padded = np.pad(x, (pad, size - 1 - pad), mode="reflect")
    kernel = np.ones(size, dtype="float32") / size
    return np.convolve(padded, kernel, mode="valid").astype("float32")


def _moving_max(x, size):
    """maximum_filter1d: centred sliding maximum."""
    import numpy as np
    size = max(1, int(size))
    if size == 1:
        return x.astype("float32", copy=True)
    pad = size // 2
    padded = np.pad(x, (pad, size - 1 - pad), mode="edge")
    return np.max(np.lib.stride_tricks.sliding_window_view(padded, size),
                  axis=-1).astype("float32")


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
    loc = _moving_max(flux, 7)
    thr = _moving_avg(flux, int(0.5 * sr / H)) * 1.6 + 0.02
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


# Which score tracks correspond to which separated stem. A stem is only useful
# for alignment if the score actually notates that instrument.
STEM_TRACKS = {
    "drums":  ("drumKit",),
    "bass":   ("acousticBass",),
    "vocals": ("voice",),
    # demucs lumps guitars and keys into "other", and those are usually the
    # densest parts in the score, which makes this a strong extra vote.
    "other":  ("electricGuitar", "acousticPiano", "leadSynthesizer"),
}


def score_onsets(gp_path, limit=None, types=("drumKit",)):
    """Every note onset in the score for the given instrument types, in seconds."""
    return _walk_drums(gp_path, collect_all=True, limit=limit, types=types)


def first_note_time(gp_path):
    """Seconds from bar 1 to the first sounding note of the drum track."""
    hits = _walk_drums(gp_path, collect_all=False)
    return hits[0] if hits else None


def _walk_drums(gp_path, collect_all, limit=None, types=("drumKit",)):
    out = []
    with zipfile.ZipFile(str(gp_path)) as z:
        root = ET.fromstring(z.read("Content/score.gpif").decode("utf-8"))
    tracks = list(root.find("Tracks"))
    wanted = []
    for i, t in enumerate(tracks):
        iset = t.find("InstrumentSet")
        if iset is not None and iset.findtext("Type") in types:
            wanted.append(i)
    if not wanted:
        return []

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
        for ti in wanted:
            if ti >= len(bar_ids) or bar_ids[ti] == "-1":
                continue
            bar = bars[int(bar_ids[ti])]
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


def _onset_flux(wav_path, window):
    """Normalised spectral-flux envelope of a stem, plus its frame rate."""
    import numpy as np
    x, sr = _read_wav(wav_path)
    x = x.mean(axis=1)[: int(window * sr)]
    H, N = 512, 2048
    fps = sr / H
    frames = 1 + (len(x) - N) // H
    if frames < 16:
        return None, fps
    win = np.hanning(N).astype("float32")
    S = np.empty((frames, N // 2 + 1), dtype="float32")
    for i in range(frames):
        S[i] = np.abs(np.fft.rfft(x[i * H:i * H + N] * win))
    flux = np.maximum(0, np.diff(S, axis=0)).sum(axis=1)
    if flux.max() <= 0:
        return None, fps
    flux = flux / flux.max()
    flux -= _moving_avg(flux, int(fps))          # de-trend
    return flux, fps


def _corr_curve(flux, onsets, fps, max_lag):
    """Normalised correlation of a stem's envelope against notated onsets.

    Returns the whole curve, not just its peak. Repetitive music produces many
    near-equal peaks roughly a phrase apart, so which one wins is close to
    arbitrary for a single instrument -- the curve is what lets several stems
    be combined before anything is decided.
    """
    import numpy as np
    spikes = np.zeros(len(flux), dtype="float32")
    for t in onsets:
        i = int(round(t * fps))
        if 0 <= i < len(spikes):
            spikes[i] = 1.0
    if spikes.sum() < 4:
        return None
    spikes = _moving_avg(spikes, 3)
    spikes -= spikes.mean()

    lags = int(max_lag * fps)
    corr = np.correlate(flux, spikes, mode="full")
    mid = len(spikes) - 1                    # index of zero lag
    lo, hi = mid - lags, mid + lags + 1
    seg = corr[max(0, lo): min(len(corr), hi)]
    if len(seg) < 3:
        return None
    # Pad back to a fixed width so every stem's curve shares one lag axis.
    out = np.zeros(2 * lags + 1, dtype="float32")
    out[max(0, -lo): max(0, -lo) + len(seg)] = seg
    # Smooth over ~100 ms before anything is compared. Bass, guitar and drums do
    # not attack at the identical millisecond, so demanding exact coincidence
    # between their curves throws away the agreement we are looking for.
    out = _moving_avg(out, max(3, int(0.10 * fps)))
    med = float(np.median(np.abs(out)))
    return out / med if med > 0 else None


def _peak(curve, fps, lags):
    import numpy as np
    i = int(np.argmax(curve))
    med = float(np.median(np.abs(curve))) or 1.0
    return (i - lags) / fps, float(curve[i] / med)


def _correlate(flux, onsets, fps, max_lag):
    """Single-stem peak. Kept for callers that only want one number."""
    curve = _corr_curve(flux, onsets, fps, max_lag)
    if curve is None:
        return None, 0.0
    return _peak(curve, fps, int(max_lag * fps))


def estimate_offset(gp_path, drum_stem, max_lag=30.0, window=180.0):
    """Backwards-compatible single-stem estimate."""
    res = estimate_offset_multi(gp_path, {"drums": drum_stem}, max_lag, window)
    return res["offset"], res["confidence"]


AGREE_TOLERANCE = 0.060      # 60 ms: stems this close are describing one offset
MIN_ONSETS = 24              # below this a stem cannot vote


def estimate_offset_multi(gp_path, stems, max_lag=30.0, window=180.0):
    """Offset in seconds, decided by every stem at once.

    Each stem is correlated against the notes its own instrument plays, and the
    normalised curves are summed before any peak is chosen. A spurious peak sits
    at a different lag in each stem and averages away; the true offset is the
    one lag they all support, so it survives. Deciding per stem and then voting
    on the winners cannot do this -- by then the ambiguity has already been
    resolved, arbitrarily, one stem at a time.

    Returns a dict: offset, confidence, agreement, per-stem detail.
    """
    import numpy as np
    lags = int(max_lag * (44100 / 512))
    curves, votes = {}, {}
    fps_used = None
    for name, path in (stems or {}).items():
        if not path or not os.path.exists(path):
            continue
        types = STEM_TRACKS.get(name)
        if not types:
            continue
        onsets = score_onsets(gp_path, limit=window, types=types)
        if len(onsets) < MIN_ONSETS:
            votes[name] = {"offset": None, "confidence": 0.0, "onsets": len(onsets),
                           "why": "only %d notated notes" % len(onsets)}
            continue
        flux, fps = _onset_flux(path, window)
        if flux is None:
            votes[name] = {"offset": None, "confidence": 0.0, "onsets": len(onsets),
                           "why": "stem is silent or too short"}
            continue
        lags = int(max_lag * fps)
        fps_used = fps
        curve = _corr_curve(flux, onsets, fps, max_lag)
        if curve is None:
            votes[name] = {"offset": None, "confidence": 0.0, "onsets": len(onsets),
                           "why": "no usable peak"}
            continue
        curves[name] = curve
        off, conf = _peak(curve, fps, lags)
        votes[name] = {"offset": off, "confidence": conf,
                       "onsets": len(onsets), "why": ""}

    if not curves:
        return {"offset": None, "confidence": 0.0, "agreement": 0, "total": 0,
                "votes": votes, "method": "none"}

    n = min(len(c) for c in curves.values())
    total = np.zeros(n, dtype="float64")
    for c in curves.values():
        total += c[:n]
    offset, confidence = _peak(total, fps_used, lags)

    # How many stems independently support the lag actually chosen?
    agree = sum(1 for v in votes.values()
                if v["offset"] is not None and abs(v["offset"] - offset) <= AGREE_TOLERANCE)
    if len(curves) > 1:
        confidence *= (1.0 + 0.4 * (agree - 1)) if agree > 1 else 0.5
    return {"offset": offset, "confidence": confidence, "agreement": agree,
            "total": len(curves), "votes": votes,
            "method": "+".join(sorted(curves)) + " (summed)"}


def _tempo_regions(gp_path):
    """Score tempo map as [(bar, bpm)], plus seconds-from-start for each bar."""
    with zipfile.ZipFile(str(gp_path)) as z:
        root = ET.fromstring(z.read("Content/score.gpif").decode("utf-8"))
    tempos = []
    for a in root.iter("Automation"):
        if a.findtext("Type") == "Tempo":
            tempos.append((int(a.findtext("Bar")), float(a.findtext("Value").split()[0])))
    tempos.sort()
    if not tempos:
        return [], []

    def bpm_at(bar):
        cur = tempos[0][1]
        for b, v in tempos:
            if b <= bar:
                cur = v
            else:
                break
        return cur

    starts, seconds = [], 0.0
    for mi, mb in enumerate(root.find("MasterBars")):
        starts.append(seconds)
        num, den = (int(x) for x in mb.findtext("Time").split("/"))
        seconds += float(F(num, den)) * (4 * 60.0 / bpm_at(mi))
    starts.append(seconds)                      # end of the last bar
    return tempos, starts


def fit_tempo_map(gp_path, stems, window=600.0, max_lag=30.0):
    """Measure the recording's real tempo for each of the score's tempo regions.

    Guitar Pro plays the score at the score's tempos while the backing track
    plays at its own; a single FramePadding can only make them agree at one
    point. If the transcriber's tempos are approximate -- and they usually are,
    115/120/125 being a person's estimate -- the audio drifts away at every
    tempo change no matter where the start is nudged to.

    So rather than aligning once, each region is aligned on its own and the
    score's tempo values are corrected to match how long that region actually
    lasts in the recording. Returns (padding_seconds, [(bar, corrected_bpm)],
    detail) or (None, [], detail) when there is not enough to measure.
    """
    import numpy as np
    tempos, starts = _tempo_regions(gp_path)
    detail = {"regions": [], "changed": False}
    if len(tempos) < 1 or not starts:
        return None, [], detail

    # Onsets per stem, once.
    fluxes = {}
    onsets = {}
    for name, path in (stems or {}).items():
        if not path or not os.path.exists(path):
            continue
        types = STEM_TRACKS.get(name)
        if not types:
            continue
        o = score_onsets(gp_path, limit=window, types=types)
        if len(o) < MIN_ONSETS:
            continue
        flux, fps = _onset_flux(path, window)
        if flux is None:
            continue
        fluxes[name] = (flux, fps)
        onsets[name] = o
    if not fluxes:
        return None, [], detail

    bounds = [starts[min(b, len(starts) - 1)] for b, _v in tempos] + [starts[-1]]

    def offset_between(t0, t1, centre=None, span=2.0):
        """Consensus lag using only the notes inside a score-time span.

        A short span carries few onsets and correlates weakly, so once one
        region has been located the rest are searched only near it. Scanning
        the full lag range for every region let a 36-second outro lock onto a
        peak 19 seconds away and report that as tempo drift.
        """
        total, fps_used, lags, votes = None, None, None, 0
        for name, (flux, fps) in fluxes.items():
            sel = [t for t in onsets[name] if t0 <= t < t1]
            if len(sel) < MIN_ONSETS:
                continue
            curve = _corr_curve(flux, sel, fps, max_lag)
            if curve is None:
                continue
            lags = int(max_lag * fps)
            fps_used = fps
            total = curve if total is None else total[:len(curve)] + curve[:len(total)]
            votes += 1
        if total is None:
            return None, 0.0, 0
        if centre is not None:
            lo = max(0, int((centre - span) * fps_used) + lags)
            hi = min(len(total), int((centre + span) * fps_used) + lags + 1)
            if hi - lo >= 3:
                sub = total[lo:hi]
                i = int(np.argmax(sub))
                med = float(np.median(np.abs(total))) or 1.0
                return (lo + i - lags) / fps_used, float(sub[i] / med), votes
        off, conf = _peak(total, fps_used, lags)
        return off, conf, votes

    offs = []
    anchor = None
    for i in range(len(bounds) - 1):
        # The first measurable region is found from scratch; the rest are looked
        # for near whatever the previous one settled on.
        o, c, v = offset_between(bounds[i], bounds[i + 1], centre=anchor)
        if o is not None:
            anchor = o
        offs.append(o)
        detail["regions"].append({"bar": tempos[i][0], "bpm": tempos[i][1],
                                  "offset": o, "confidence": c, "stems": v,
                                  "span": (bounds[i], bounds[i + 1])})

    if all(o is None for o in offs):
        return None, [], detail
    # A short opening region -- a two-bar intro, say -- carries too few notes to
    # correlate. Rather than giving up on the whole song, borrow the first
    # region that could be measured.
    first = next(o for o in offs if o is not None)
    offs = [first if o is None and i < offs.index(first) else o
            for i, o in enumerate(offs)]

    # The final region has no boundary after it to compare against, so measure
    # the drift across its own two halves instead. Without this the last tempo
    # -- often a whole outro -- can never be corrected.
    tail_end = None
    lo, hi = bounds[-2], bounds[-1]
    if hi - lo > 30.0 and offs[-1] is not None:
        mid = (lo + hi) / 2
        a, ca, _v1 = offset_between(lo, mid, centre=offs[-1], span=1.0)
        b, cb, _v2 = offset_between(mid, hi, centre=offs[-1], span=1.0)
        # Drift across half a region can only be small; anything larger is a bad
        # correlation, not a tempo error, so leave the tempo alone.
        if (a is not None and b is not None and min(ca, cb) > 2.0
                and abs(b - a) < 0.05 * (hi - lo)):
            offs[-1] = a
            tail_end = b + (b - a) * 0.5
            detail["tail_drift"] = b - a

    # Carry the previous measurement through regions too sparse to measure, so
    # one quiet section does not throw away the ones after it.
    filled = []
    last = offs[0]
    for o in offs:
        last = o if o is not None else last
        filled.append(last)
    filled.append(tail_end if tail_end is not None else filled[-1])

    corrected = []
    for i, (bar, bpm) in enumerate(tempos):
        score_len = bounds[i + 1] - bounds[i]
        audio_len = (bounds[i + 1] + filled[i + 1]) - (bounds[i] + filled[i])
        if score_len <= 0 or audio_len <= 0:
            corrected.append((bar, bpm))
            continue
        ratio = score_len / audio_len
        # Refuse absurd corrections: beyond a few percent this is a bad
        # correlation, not a transcriber's rounding.
        if not (0.90 <= ratio <= 1.10):
            corrected.append((bar, bpm))
            continue
        corrected.append((bar, round(bpm * ratio, 3)))

    detail["changed"] = any(abs(c - o) > 0.05 for (_b, c), (_b2, o) in zip(corrected, tempos))
    return filled[0], corrected, detail


def write_tempo_map(gp_path, tempos, out_path):
    """Rewrite the score's tempo automations, keeping everything else."""
    with zipfile.ZipFile(str(gp_path)) as z:
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
    x = blobs["Content/score.gpif"].decode("utf-8")
    autos = "".join(
        "<Automation><Type>Tempo</Type><Linear>false</Linear><Bar>%d</Bar>"
        "<Position>0</Position><Visible>true</Visible><Value>%g 2</Value></Automation>"
        % (bar, bpm) for bar, bpm in tempos)
    x = re.sub(r"<Automations>.*?</Automations>", "<Automations>" + autos + "</Automations>",
               x, count=1, flags=re.S)
    blobs["Content/score.gpif"] = x.encode("utf-8")
    with zipfile.ZipFile(str(out_path), "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, blobs[n])
    return out_path


def align_and_embed(gp_path, drum_stem, audio_path, out_path, log=print,
                    stems=None, fit_tempo=True):
    """Align *audio_path* to the score and embed it.

    *stems* maps stem name -> wav path (drums / bass / other / vocals). The more
    of them exist, the more independent votes the offset gets.

    With *fit_tempo*, the score's tempo markings are also corrected to match how
    long each section actually lasts in the recording. Guitar Pro plays the
    score at the score's tempos while the audio plays at its own, so a single
    padding value can only make them agree at one point: if the transcriber's
    tempos are approximate the audio drifts away at every tempo change, however
    carefully the start is nudged.
    """
    pool = dict(stems or {})
    if drum_stem and "drums" not in pool:
        pool["drums"] = drum_stem

    source = gp_path
    offset = None
    tmp_gp = None

    if fit_tempo:
        try:
            pad, corrected, detail = fit_tempo_map(gp_path, pool)
        except Exception as e:
            pad, corrected, detail = None, [], {"error": str(e)}
            log("  ⚠ tempo fitting failed (%s); falling back to a single offset" % e)
        for r in detail.get("regions", []):
            if r["offset"] is None:
                log("  bar %-4d %-7s not measurable" % (r["bar"], "%gbpm" % r["bpm"]))
            else:
                log("  bar %-4d %-7s audio is %+.0f ms away (confidence %.1fx, %d stem(s))"
                    % (r["bar"], "%gbpm" % r["bpm"], r["offset"] * 1000,
                       r["confidence"], r["stems"]))
        if pad is not None:
            offset = pad
            if detail.get("changed"):
                changes = [(b, c) for (b, c), (_b, o) in
                           zip(corrected, _tempo_regions(gp_path)[0]) if abs(c - o) > 0.05]
                log("  adjusted %d tempo marking(s) to match the recording: %s"
                    % (len(changes), ", ".join("bar %d → %g bpm" % c for c in changes)))
                fd, tmp_gp = tempfile.mkstemp(suffix=".gp")
                os.close(fd)
                write_tempo_map(gp_path, corrected, tmp_gp)
                source = tmp_gp

    if offset is None:
        res = estimate_offset_multi(gp_path, pool)
        offset, conf = res["offset"], res["confidence"]
        for name, v in sorted(res["votes"].items()):
            if v["offset"] is None:
                log("  %-6s no vote (%s)" % (name, v["why"] or "unusable"))
            else:
                log("  %-6s %+.0f ms from %d notated notes (peak %.1fx)"
                    % (name, v["offset"] * 1000, v["onsets"], v["confidence"]))
        if offset is None:
            log("  ⚠ could not align from any stem — embedding at 0")
            offset = 0.0
        else:
            agree, total = res.get("agreement", 1), res.get("total", 1)
            log("  offset %+.0f ms — %d of %d stems agree, confidence %.1fx"
                % (offset * 1000, agree, total, conf))
            if total > 1 and agree < total:
                log("  ⚠ stems disagree; the recording may drift against the score, "
                    "so one offset will not hold throughout")
    else:
        log("  offset %+.0f ms" % (offset * 1000))

    if offset < 0:
        log("  ⚠ audio starts before the score; clamping to 0")
    try:
        return embed(source, audio_path, out_path,
                     padding_frames=round(max(0.0, offset) * SAMPLE_RATE))
    finally:
        if tmp_gp:
            try:
                os.unlink(tmp_gp)
            except OSError:
                pass
