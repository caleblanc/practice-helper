#!/usr/bin/env python3
"""
Songsterr track data -> Guitar Pro 8 (.gp), written directly.

Replaces the old  JSON -> MIDI -> GP5  chain, which lost:
  * tuning        (midi_to_gp._tuning_for guessed it from pitch range)
  * string / fret (MIDI has no such concept; frets were re-derived)
  * exact rhythm  (midi_to_gp._detect_feel snapped each track onto a single
                   16th or 8th-triplet grid, destroying 32nds and triplets)

Songsterr's data carries all of it, so nothing is inferred here.
"""

import os, re, json, shutil, zipfile, collections
from fractions import Fraction as F
import xml.etree.ElementTree as ET

TEMPLATE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gp_template")
NOTE_VALUE = {1:"Whole",2:"Half",4:"Quarter",8:"Eighth",16:"16th",32:"32nd",64:"64th"}
STEPS      = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
DRUM_ID    = 1024
# Bump whenever the written output changes, so the app regenerates stale files
# instead of silently serving one built by an older converter.
WRITER_VERSION = "14"
STAMP = "songsterr_to_gp v" + WRITER_VERSION
# A tremolo covering only a few subdivisions is a real burst (a drum double
# stroke, e.g. a 16th played as two 32nds) and gets written out. Anything longer
# is sustained tremolo picking, which is conventionally a slashed stem.
TREMOLO_EXPAND_MAX = 4

# Songsterr transcriptions often use only a couple of tom notes, and they cluster
# (e.g. both floor toms). Spread whatever is actually used across GP's tom
# positions so they read as distinct drums. Listed high -> low, matching the
# convention already used by okw_metal_to_gp8.py.
GM_TOMS = [41, 43, 45, 47, 48, 50]
TOM_SPREAD = {1: [48], 2: [48, 41], 3: [48, 47, 41], 4: [48, 47, 43, 41],
              5: [50, 48, 47, 43, 41], 6: [50, 48, 47, 45, 43, 41]}
# A kick landing this soon after the previous one is a double-pedal stroke, so
# alternate feet (36 = right, 35 = left) the way a drum chart would notate it.
DOUBLE_KICK_GAP = F(1, 16)
KICK_R, KICK_L = 36, 35
DYN        = {"ppp":"PPP","pp":"PP","p":"P","mp":"MP","mf":"MF","f":"F","ff":"FF","fff":"FFF"}

CANDS = []
for _nv in (1,2,4,8,16,32,64):
    CANDS.append((F(1,_nv), _nv, 0, False))
    CANDS.append((F(3,2*_nv), _nv, 1, False))
    CANDS.append((F(7,4*_nv), _nv, 2, False))
    CANDS.append((F(2,3*_nv), _nv, 0, True))
NEUTRAL_STRIP = "0.5 0.5 0.5 0.5 0.5 0.5 0.5 0.5 0.5 0 0.5 0.5 0.795 0.5 0.5 0.5"
HARMONIC = {"natural":"Natural","artificial":"Artificial","pinch":"Pinch",
            "tap":"Tap","semi":"Semi","feedback":"Feedback"}
SLIDE = {"shift":1, "legato":2,
         "downwards":4, "out_down":4, "outdown":4,
         "upwards":8,   "out_up":8,   "outup":8,
         "aboveupwards":16, "abovedownwards":16, "in_above":16, "inabove":16,
         "belowupwards":32, "belowdownwards":32, "in_below":32, "inbelow":32}
# Fields we deliberately consume; anything else gets reported so gaps surface.
KNOWN_NOTE = {"rest","string","fret","tie","dead","harmonic","harmonicFret","slide",
              "hp","ghost","accentuated","leftHandVibrato","bend"}
KNOWN_BEAT = {"notes","duration","rest","velocity","letRing","palmMute","tremolo",
              "dots","type","tuplet","tupletStart","tupletStop","beamStart","beamStop","text"}
CANDS.sort(key=lambda c: -c[0])
PLAIN = [c for c in CANDS if not c[2] and not c[3]]
EXACT = {c[0]: c[1:] for c in CANDS}


def archetype_for(iid):
    if iid == DRUM_ID:  return "drumKit"
    if 32 <= iid <= 39: return "acousticBass"
    if 24 <= iid <= 31: return "electricGuitar"
    if 0  <= iid <= 7:  return "acousticPiano"
    if 40 <= iid <= 55: return "voice"
    return "leadSynthesizer"


def _fill(start, end):
    out, p = [], start
    while p < end:
        pick = None
        for d,nv,dots,tup in PLAIN:
            if d <= end-p and p % d == 0: pick=(nv,dots,tup); p+=d; break
        if pick is None:
            for d,nv,dots,tup in CANDS:
                if d <= end-p: pick=(nv,dots,tup); p+=d; break
        if pick is None: raise ValueError("cannot fill %s..%s"%(start,end))
        out.append(pick)
    return out


def _artmap(drum_xml):
    """GM note -> articulation index.

    Two passes on purpose. Several GP kit elements emit a different note than
    they accept (e.g. "Tom Very High" outputs 43), so interleaving inputs and
    outputs lets an early element's *output* steal a GM note from the element
    that legitimately takes it as an *input* -- which is what put the high
    floor tom on the top staff line.
    """
    el = ET.fromstring(drum_xml)
    arts = [(a, i) for i, a in enumerate(
        a for e in el.find("InstrumentSet").find("Elements")
        for a in e.find("Articulations"))]
    amap = {}
    for a, i in arts:                                    # declared inputs win
        for n in (a.findtext("InputMidiNumbers") or "").split():
            amap.setdefault(int(n), i)
    for a, i in arts:                                    # outputs only fill gaps
        o = a.findtext("OutputMidiNumber")
        if o:
            amap.setdefault(int(o), i)

    # GP's stock kit declares 41 above 43, inverting the two floor toms. Keep the
    # tom ladder monotonic so a higher GM note is never drawn below a lower one.
    line = {}
    for a, i in arts:
        try: line[i] = int(a.findtext("StaffLine"))
        except (TypeError, ValueError): pass
    toms = [n for n in GM_TOMS if n in amap]
    slots = sorted((amap[n] for n in toms), key=lambda i: -line.get(i, 0))
    for n, i in zip(toms, slots):                        # low GM -> low on staff
        amap[n] = i
    return amap


def _pitch_xml(tag, midi):
    s = STEPS[midi % 12]
    return ("<Property name=\"%s\"><Pitch><Step>%s</Step><Accidental>%s</Accidental>"
            "<Octave>%d</Octave></Pitch></Property>" % (tag, s[0], s[1:], midi//12))


def _sub(xml, tag, val):
    return re.sub(r"<%s><!\[CDATA\[.*?\]\]></%s>" % (tag,tag),
                  "<%s><![CDATA[%s]]></%s>" % (tag,val,tag), xml, count=1, flags=re.S)


def convert(revision, track_data, dest_path, notation="standard"):
    warn  = []
    stats = collections.Counter()
    skel  = open(os.path.join(TEMPLATE,"Content","score.gpif"), encoding="utf-8").read()
    arch  = {n[:-4]: open(os.path.join(TEMPLATE,"archetypes",n), encoding="utf-8").read()
             for n in os.listdir(os.path.join(TEMPLATE,"archetypes")) if n.endswith(".xml")}
    amap  = _artmap(arch["drumKit"])

    tracks = [td for td in track_data if td.get("measures")]
    if not tracks: raise ValueError("no tracks with measures")

    nbars = max(len(td["measures"]) for td in tracks)
    sigs = {}
    for td in tracks:
        for i,m in enumerate(td["measures"]):
            if "signature" in m and i not in sigs:
                sigs[i] = (int(m["signature"][0]), int(m["signature"][1]))
    barsig, cur = [], (4,4)
    for i in range(nbars):
        cur = sigs.get(i, cur); barsig.append(cur)

    rhythms, beatpool, notepool = {}, {}, {}
    rid = lambda k: rhythms.setdefault(k, len(rhythms))
    nid = lambda k: notepool.setdefault(k, len(notepool))
    bid = lambda k: beatpool.setdefault(k, len(beatpool))

    voices, bars, per_track_bars = [], [], []

    clefs = []
    for td in tracks:
        drum   = td.get("instrumentId") == DRUM_ID
        tuning = list(td.get("tuning") or [])
        nstr   = int(td.get("strings") or (len(tuning) or 6))
        tom_map, kick_plan = {}, []
        if td.get("instrumentId") == DRUM_ID:
            tom_map = {}          # positions now match the source 1:1

            kick_plan = _kick_feet(td, barsig)
        kick_seen = [0]
        kind_  = archetype_for(td.get("instrumentId", -1))
        clef   = "Neutral" if drum else ("F4" if kind_ == "acousticBass" else "G2")
        clefs.append(clef)
        tbars  = []
        last_on_string = collections.defaultdict(dict)   # voice slot -> {string: note}
        for mi in range(nbars):
            num, den = barsig[mi]
            tgt  = F(num, den)
            meas = td["measures"][mi] if mi < len(td["measures"]) else {}
            slot = []
            for vi, v in enumerate((meas.get("voices") or [])[:4]):
                if not isinstance(v, dict): continue
                seq, pos = [], F(0)
                for b in (v.get("beats") or []):
                    dur = b.get("duration")
                    if not dur: continue
                    frac = F(int(dur[0]), int(dur[1]))
                    if pos + frac > tgt:
                        frac = tgt - pos
                        stats["truncated_at_barline"] += 1
                        if frac <= 0: break
                    if frac not in EXACT:
                        for nv,dots,tup in _fill(pos, pos+frac):
                            seq.append(((nv,dots,tup), [], "MF", ""))
                        stats["unrepresentable_duration"] += 1; pos += frac; continue
                    nv,dots,tup = EXACT[frac]
                    ns = []
                    if not b.get("rest"):
                        for n in (b.get("notes") or []):
                            if n.get("rest"): continue
                            if drum:
                                midi = int(n.get("fret", 0))
                                midi = tom_map.get(midi, midi)
                                if midi == KICK_R:
                                    i = kick_seen[0]; kick_seen[0] += 1
                                    if i < len(kick_plan) and kick_plan[i]:
                                        midi = KICK_L
                                        stats["kick_left_foot"] += 1
                                if midi not in amap:
                                    midi = min(amap, key=lambda k:(abs(k-midi),k))
                                    stats["drum_substituted"] += 1
                                nd = {"k":"d", "art":amap[midi], "midi":midi,
                                      "ghost":bool(n.get("ghost")),
                                      "accent":int(n.get("accentuated") or 0),
                                      "tie_dst":bool(n.get("tie")), "tie_org":False}
                                prev = last_on_string[vi].get(("d", midi))
                                if prev is not None and nd["tie_dst"]:
                                    prev["tie_org"] = True
                                last_on_string[vi][("d", midi)] = nd
                                ns.append(nd)
                            else:
                                if "string" not in n or "fret" not in n:
                                    stats["note_missing_string_fret"] += 1; continue
                                si = int(n["string"]); fr = int(n["fret"])
                                if not (0 <= si < nstr) or not tuning:
                                    stats["bad_string_index"] += 1; continue
                                bend = n.get("bend") or None
                                bkey = ""
                                if bend and bend.get("points"):
                                    pts = bend["points"]
                                    bkey = "|".join("%s,%s" % (pt.get("tone",0),
                                                    pt.get("precisePosition",0)) for pt in pts)
                                art = (bool(n.get("dead")),
                                       str(n.get("harmonic") or ""),
                                       str(n.get("harmonicFret") or ""),
                                       str(n.get("slide") or ""),
                                       bool(b.get("letRing")),
                                       bool(b.get("palmMute")),
                                       bool(n.get("hp")),
                                       bool(n.get("ghost")),
                                       int(n.get("accentuated") or 0),
                                       bkey)
                                for k in n:
                                    if k not in KNOWN_NOTE: stats["unmapped_note_" + k] += 1
                                for k in b:
                                    if k not in KNOWN_BEAT: stats["unmapped_beat_" + k] += 1
                                if n.get("harmonic") and str(n["harmonic"]).lower() not in HARMONIC:
                                    stats["unknown_harmonic"] += 1
                                if n.get("slide") and str(n["slide"]).lower() not in SLIDE:
                                    stats["unknown_slide"] += 1
                                if n.get("leftHandVibrato"):
                                    stats["vibrato_not_written"] += 1
                                nd = {"k":"p", "gs":(nstr-1)-si, "fr":fr, "si":si,
                                      "midi":tuning[si]+fr, "art":art,
                                      "tie_dst":bool(n.get("tie")),
                                      "hp_src":bool(n.get("hp")),
                                      "hopo_dst":False,
                                      "tie_org":False, "hopo_org":False}
                                prev = last_on_string[vi].get(si)
                                if prev is not None:
                                    # tie marks the note tied *from* the previous one
                                    if nd["tie_dst"]: prev["tie_org"] = True
                                    # hp marks the note hammered/pulled *to the next*,
                                    # so the slur runs prev -> this note
                                    if prev.get("hp_src"):
                                        prev["hopo_org"] = True
                                        nd["hopo_dst"]   = True
                                last_on_string[vi][si] = nd
                                ns.append(nd)
                            stats["notes"] += 1
                    dy = DYN.get(str(b.get("velocity","")).lower(), "MF")
                    tv = b.get("tremolo")
                    # tremolo [1,N] means "play this beat as 1/N notes". Write them
                    # out rather than using GP's tremolo-picking property, so the
                    # subdivision is real (and audible) instead of a slashed stem.
                    sub = F(int(tv[0]), int(tv[1])) if tv else None
                    reps = int(frac / sub) if sub and sub > 0 and frac % sub == 0 else 0
                    if 1 < reps <= TREMOLO_EXPAND_MAX and sub in EXACT:
                        snv, sdots, stup = EXACT[sub]
                        for r in range(reps):
                            copy = ns if r == 0 else [dict(x) for x in ns]
                            if r:                       # only the first keeps tie/hopo links
                                for x in copy:
                                    if x["k"] == "p":
                                        x.update(tie_dst=False, tie_org=False,
                                                 hopo_dst=False, hopo_org=False)
                            seq.append(((snv, sdots, stup), copy, dy, ""))
                        stats["tremolo_expanded"] += reps
                    else:
                        if tv: stats["tremolo_as_picking"] += 1
                        seq.append(((nv,dots,tup), ns, dy,
                                    "%d/%d" % (int(tv[0]), int(tv[1])) if tv else ""))
                    pos += frac
                if pos < tgt:
                    for nv,dots,tup in _fill(pos, tgt):
                        seq.append(((nv,dots,tup), [], "MF", ""))
                if seq:
                    voices.append(seq); slot.append(len(voices)-1)
            if not slot:
                seq = [((nv,dots,tup), [], "MF", "") for nv,dots,tup in _fill(F(0), tgt)]
                voices.append(seq); slot.append(len(voices)-1)
            while len(slot) < 4: slot.append(-1)
            bars.append((slot, clef)); tbars.append(len(bars)-1)
        per_track_bars.append(tbars)

    # Every note now knows whether it is a tie/hammer-on origin, so pool them.
    def _notekey(d):
        if d["k"] == "d":
            return ("d", d["art"], d["midi"], d.get("ghost", False),
                    d.get("accent", 0), d.get("tie_dst", False), d.get("tie_org", False))
        return ("p", d["gs"], d["fr"], d["midi"], d["art"],
                d["tie_dst"], d["tie_org"], d["hopo_dst"], d["hopo_org"])
    voices = [[bid((rid(rk), tuple(nid(_notekey(x)) for x in ns), dy, tr))
               for rk, ns, dy, tr in seq] for seq in voices]

    # ---- tracks XML
    txml, chan = [], 0
    for ti, td in enumerate(tracks):
        kind = archetype_for(td.get("instrumentId", -1))
        t = arch.get(kind, arch["leadSynthesizer"])
        t = re.sub(r'<Track id="\d+">', '<Track id="%d">' % ti, t, count=1)
        t = _sub(t, "Name", (td.get("name") or "Track %d" % (ti+1)))
        if kind != "drumKit":
            tun = list(td.get("tuning") or [])
            if tun:
                t = re.sub(r"(<Property name=\"Tuning\">\s*<Pitches>)[^<]*(</Pitches>)",
                           r"\g<1>%s\g<2>" % " ".join(str(p) for p in reversed(tun)), t, count=1)
        c = 9 if kind == "drumKit" else chan
        if kind != "drumKit":
            chan += 1
            if chan == 9: chan += 1
        t = re.sub(r"(<MidiConnection>.*?<PrimaryChannel>)\d+(</PrimaryChannel>\s*<SecondaryChannel>)\d+",
                   r"\g<1>%d\g<2>%d" % (c,c), t, count=1, flags=re.S)
        t = re.sub(r"<PlaybackState>[^<]*</PlaybackState>",
                   "<PlaybackState>Default</PlaybackState>", t)
        t = re.sub(r"(<ChannelStrip[^>]*>\s*<Parameters>)[^<]*(</Parameters>)",
                   r"\g<1>" + NEUTRAL_STRIP + r"\g<2>", t, count=1)
        t = re.sub(r"<SystemsLayout>[^<]*</SystemsLayout>",
                   "<SystemsLayout>%s</SystemsLayout>" % " ".join(["4"]*((nbars+3)//4)), t)
        txml.append(t)

    X = ["<MasterBars>"]
    for mi in range(nbars):
        X.append("<MasterBar>")
        X.append("<Key><AccidentalCount>0</AccidentalCount><Mode>Major</Mode>"
                 "<TransposeAs>Sharps</TransposeAs></Key>")
        X.append("<Time>%d/%d</Time>" % barsig[mi])
        X.append("<Bars>%s</Bars>" % " ".join(str(per_track_bars[t][mi]) for t in range(len(tracks))))
        X.append("</MasterBar>")
    X.append("</MasterBars>")
    X.append("<Bars>")
    for i,(slot,clef) in enumerate(bars):
        X.append('<Bar id="%d"><Clef>%s</Clef><Voices>%s</Voices></Bar>'
                 % (i, clef, " ".join(map(str,slot))))
    X.append("</Bars>")
    X.append("<Voices>")
    for i,seq in enumerate(voices):
        X.append('<Voice id="%d"><Beats>%s</Beats></Voice>' % (i, " ".join(map(str,seq))))
    X.append("</Voices>")
    X.append("<Beats>")
    for (r,ns,dy,trem),b in sorted(beatpool.items(), key=lambda kv: kv[1]):
        X.append('<Beat id="%d"><Dynamic>%s</Dynamic><Rhythm ref="%d" />' % (b,dy,r))
        if trem: X.append("<Tremolo>%s</Tremolo>" % trem)
        if ns: X.append("<Notes>%s</Notes>" % " ".join(map(str,ns)))
        X.append("</Beat>")
    X.append("</Beats>")
    X.append("<Notes>")
    for key,n in sorted(notepool.items(), key=lambda kv: kv[1]):
        X.append('<Note id="%d"><Velocity>127</Velocity>' % n)
        if key[0] == "d":
            _, art, midi, dghost, daccent, dtie_dst, dtie_org = key
            X.append("<InstrumentArticulation>%d</InstrumentArticulation>" % art)
            X.append("<Properties>")
            X.append(_pitch_xml("ConcertPitch", 0).replace("Octave>0<","Octave>-1<"))
            X.append('<Property name="Fret"><Fret>%d</Fret></Property>' % midi)
            X.append('<Property name="Midi"><Number>%d</Number></Property>' % midi)
            X.append('<Property name="String"><String>%d</String></Property>' % (art % 7))
            X.append(_pitch_xml("TransposedPitch", 0).replace("Octave>0<","Octave>-1<"))
            X.append("</Properties>")
            if dghost:  X.append("<AntiAccent>normal</AntiAccent>")
            if daccent: X.append("<Accent>8</Accent>")
            if dtie_org or dtie_dst:
                X.append('<Tie origin="%s" destination="%s" />'
                         % (str(dtie_org).lower(), str(dtie_dst).lower()))
            X.append("</Note>")
        else:
            _, gs, fr, midi, art, tie_dst, tie_org, hopo_dst, hopo_org = key
            (dead, harm, hfret, slide, letring,
             palm, _hp_unused, ghost, accent, bkey) = art
            X.append("<InstrumentArticulation>0</InstrumentArticulation>")
            X.append("<Properties>")
            X.append(_pitch_xml("ConcertPitch", midi))
            X.append('<Property name="Fret"><Fret>%d</Fret></Property>' % fr)
            X.append('<Property name="Midi"><Number>%d</Number></Property>' % midi)
            X.append('<Property name="String"><String>%d</String></Property>' % gs)
            X.append(_pitch_xml("TransposedPitch", midi))
            if dead:
                X.append('<Property name="Muted"><Enable /></Property>')
            if harm and harm.lower() in HARMONIC:
                X.append('<Property name="HarmonicType"><HType>%s</HType></Property>'
                         % HARMONIC[harm.lower()])
                if hfret:
                    X.append('<Property name="HarmonicFret"><HFret>%s</HFret></Property>' % hfret)
            if slide and slide.lower() in SLIDE:
                X.append('<Property name="Slide"><Flags>%d</Flags></Property>' % SLIDE[slide.lower()])
            if palm:
                X.append('<Property name="PalmMuted"><Enable /></Property>')
            if hopo_org:
                X.append('<Property name="HopoOrigin"><Enable /></Property>')
            if hopo_dst:
                X.append('<Property name="HopoDestination"><Enable /></Property>')
            if bkey:
                pts = [p.split(",") for p in bkey.split("|")]
                vals = [(float(a), float(o)) for a, o in pts]
                o_v, o_o = vals[0]
                d_v, d_o = vals[-1]
                mid = vals[1:-1] or [vals[-1]]
                X.append('<Property name="Bended"><Enable /></Property>')
                X.append('<Property name="BendOriginValue"><Float>%g</Float></Property>' % o_v)
                X.append('<Property name="BendOriginOffset"><Float>%g</Float></Property>' % o_o)
                X.append('<Property name="BendMiddleValue"><Float>%g</Float></Property>' % mid[0][0])
                X.append('<Property name="BendMiddleOffset1"><Float>%g</Float></Property>' % mid[0][1])
                X.append('<Property name="BendMiddleOffset2"><Float>%g</Float></Property>'
                         % (mid[-1][1] if len(mid) > 1 else mid[0][1]))
                X.append('<Property name="BendDestinationValue"><Float>%g</Float></Property>' % d_v)
                X.append('<Property name="BendDestinationOffset"><Float>%g</Float></Property>' % d_o)
            X.append("</Properties>")
            if ghost: X.append("<AntiAccent>normal</AntiAccent>")
            if accent: X.append("<Accent>8</Accent>")
            if letring: X.append("<LetRing />")
            if tie_org or tie_dst:
                X.append('<Tie origin="%s" destination="%s" />'
                         % (str(tie_org).lower(), str(tie_dst).lower()))
            X.append("</Note>")
    X.append("</Notes>")
    X.append("<Rhythms>")
    for (nv,dots,tup),r in sorted(rhythms.items(), key=lambda kv: kv[1]):
        X.append('<Rhythm id="%d"><NoteValue>%s</NoteValue>' % (r, NOTE_VALUE[nv]))
        if dots: X.append('<AugmentationDot count="%d" />' % dots)
        if tup:  X.append('<PrimaryTuplet num="3" den="2" />')
        X.append("</Rhythm>")
    X.append("</Rhythms>")

    out = skel.replace("@@MUSIC@@", "\n".join(X))
    out = out.replace("@@TRACKS@@", "\n".join(txml))
    out = out.replace("@@TRACKLIST@@", " ".join(str(i) for i in range(len(tracks))))
    out = _sub(out, "Tabber", STAMP)
    out = _sub(out, "Title",  revision.get("title","")  if revision else "")
    out = _sub(out, "Artist", revision.get("artist","") if revision else "")

    autos = []
    tempo_auto = (tracks[0].get("automations") or {}).get("tempo") or []
    for a in tempo_auto:
        autos.append("<Automation><Type>Tempo</Type><Linear>false</Linear><Bar>%d</Bar>"
                     "<Position>%g</Position><Visible>true</Visible><Value>%g 2</Value></Automation>"
                     % (int(a.get("measure",0)), 0, float(a.get("bpm",120))))
    if not autos:
        autos.append("<Automation><Type>Tempo</Type><Linear>false</Linear><Bar>0</Bar>"
                     "<Position>0</Position><Visible>true</Visible><Value>120 2</Value></Automation>")
    out = re.sub(r"<Automations>.*?</Automations>", "<Automations>"+"".join(autos)+"</Automations>",
                 out, count=1, flags=re.S)

    import struct
    n_tracks = len(tracks)
    fretted  = {"electricGuitar","acousticBass"}
    flags    = bytes(0x03 if archetype_for(td.get("instrumentId",-1)) in fretted else 0x01
                     for td in tracks)
    # PartConfiguration: u32(N+1), full-score entry, one entry per track, u32(N)
    pc  = struct.pack(">I", n_tracks + 1)
    pc += struct.pack(">I", 0) + bytes([n_tracks]) + flags
    for f in flags:
        pc += struct.pack(">I", 0) + bytes([1, f])
    pc += struct.pack(">I", n_tracks)
    # LayoutConfiguration: u32(4), 0x0100, 0xffff per track, u32(N-1)
    lc  = struct.pack(">I", 4) + b"\x01\x00" + b"\xff\xff" * n_tracks
    lc += struct.pack(">I", max(0, n_tracks - 1))

    tmp = str(dest_path) + ".tmpdir"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "Content", "ScoreViews"))
    os.makedirs(os.path.join(tmp, "Content", "Stylesheets"))
    def _cp(rel):
        shutil.copy(os.path.join(TEMPLATE, rel), os.path.join(tmp, rel))
    open(os.path.join(tmp,"VERSION"),"w").write(open(os.path.join(TEMPLATE,"VERSION")).read())
    open(os.path.join(tmp,"meta.json"),"w").write(
        json.dumps({"hasAudio": False, "version": "1.0.0"}, indent=4) + "\n")
    _cp("Content/BinaryStylesheet")
    _cp("Content/Preferences.json")
    _cp("Content/Stylesheets/score.gpss")
    _cp("Content/Stylesheets/scoreview1.gpss")
    open(os.path.join(tmp,"Content","PartConfiguration"),"wb").write(pc)
    open(os.path.join(tmp,"Content","LayoutConfiguration"),"wb").write(lc)
    # single score view, matching the one declared in the gpif
    open(os.path.join(tmp,"Content","ScoreViews","1.gpsv"),"wb").write(
        b"\x12\x0aFull score")
    open(os.path.join(tmp,"Content","score.gpif"),"w",encoding="utf-8").write(out)
    names = ["VERSION","meta.json","Content/BinaryStylesheet","Content/LayoutConfiguration",
             "Content/PartConfiguration","Content/Preferences.json",
             "Content/ScoreViews/1.gpsv","Content/Stylesheets/score.gpss",
             "Content/Stylesheets/scoreview1.gpss","Content/score.gpif"]
    with zipfile.ZipFile(str(dest_path),"w",zipfile.ZIP_DEFLATED) as z:
        for d in ("Content/","Content/ScoreViews/","Content/Stylesheets/"):
            zi = zipfile.ZipInfo(d); zi.external_attr = 0o40755 << 16; z.writestr(zi, b"")
        for n in names: z.write(os.path.join(tmp,n), n)
    shutil.rmtree(tmp, ignore_errors=True)

    for k,v in stats.items():
        if k != "notes": warn.append("%s: %d" % (k.replace("_"," "), v))
    warn.append("%d tracks, %d bars, %d notes" % (len(tracks), nbars, stats["notes"]))
    return warn


def file_version(path) -> str:
    """Writer version stamped into an existing .gp, or "" if absent/unreadable."""
    try:
        with zipfile.ZipFile(str(path)) as z:
            head = z.read("Content/score.gpif").decode("utf-8", "replace")[:8000]
        m = re.search(r"<Tabber><!\[CDATA\[songsterr_to_gp v(\d+)\]\]></Tabber>", head)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _tom_spread(td):
    """Map the tom notes a track actually uses onto spread-out GP positions.

    Only kicks in when the transcription uses three or fewer toms, which is the
    case that reads badly (e.g. both floor toms and nothing else). A chart that
    already uses four or more has a real spread of its own and is left alone.
    """
    used = sorted({int(n["fret"]) for m in td.get("measures", [])
                   for v in (m.get("voices") or []) if isinstance(v, dict)
                   for b in (v.get("beats") or [])
                   for n in (b.get("notes") or [])
                   if not n.get("rest") and int(n.get("fret", -1)) in GM_TOMS})
    if not used or len(used) > 3:
        return {}
    targets = list(reversed(TOM_SPREAD[len(used)]))      # low -> high
    return {src: dst for src, dst in zip(used, targets) if src != dst}


def _kick_feet(td, barsig):
    """True for each kick (in encounter order) that should be the left foot.

    Skipped entirely when the transcription already distinguishes feet — some
    Songsterr charts use both 35 and 36, and second-guessing those would wreck
    the transcriber's own sticking.
    """
    already = any(int(n.get("fret", -1)) == KICK_L
                  for m in td.get("measures", [])
                  for v in (m.get("voices") or []) if isinstance(v, dict)
                  for b in (v.get("beats") or [])
                  for n in (b.get("notes") or []) if not n.get("rest"))
    if already:
        return []
    times, pos = [], F(0)
    for mi, m in enumerate(td.get("measures", [])):
        num, den = barsig[mi] if mi < len(barsig) else (4, 4)
        for v in (m.get("voices") or [])[:4]:
            if not isinstance(v, dict):
                continue
            t = F(0)
            for b in (v.get("beats") or []):
                dur = b.get("duration")
                if not dur:
                    continue
                for n in (b.get("notes") or []):
                    if not n.get("rest") and int(n.get("fret", -1)) == KICK_R:
                        times.append(pos + t)
                t += F(int(dur[0]), int(dur[1]))
        pos += F(num, den)
    order = sorted(range(len(times)), key=lambda i: times[i])
    plan, left, prev = [False] * len(times), False, None
    for i in order:
        left = (not left) if (prev is not None and times[i] - prev <= DOUBLE_KICK_GAP) else False
        plan[i] = left
        prev = times[i]
    return plan
