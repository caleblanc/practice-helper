#!/usr/bin/env python3
"""
MIDI → Guitar Pro 5 converter.

Pipeline:
  1. Parse MIDI (mido): tempo map, time-sig map, PPQ, per-track note events
  2. Detect rhythmic feel per track: triplet-8th vs straight-16th
  3. Quantize note positions onto the detected grid
  4. Map GM drum pitches → GP percussion; melodic tracks → string/fret
  5. Assemble GP5 (PyGuitarPro) and write
  6. Validate: re-read, check for parse errors
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import mido
import guitarpro as gp

QUARTER = gp.Duration.quarterTime   # 960 ticks internally
MAX_FRET = 22

# ── GM percussion → GP note value ─────────────────────────────────────────────
# GP5 uses GM note numbers for drums directly, with some consolidation.
GM_TO_GP: dict[int, int] = {
    35: 36,  36: 36,   # Bass drum
    37: 37,            # Side stick / rim shot
    38: 38,  40: 38,   # Snare
    39: 38,            # Hand clap → snare
    41: 41,  43: 43,   # Low floor tom, high floor tom
    42: 42,            # Closed hi-hat
    44: 44,            # Pedal hi-hat
    45: 45,  47: 47, 48: 48, 50: 50,  # Toms
    46: 46,            # Open hi-hat
    49: 49,  57: 49,   # Crash 1 and 2
    51: 51,  59: 51,   # Ride 1 and 2
    52: 52,            # Chinese cymbal
    53: 53,            # Ride bell
    54: 54,            # Tambourine
    55: 55,            # Splash cymbal
    56: 56,            # Cowbell
    58: 38,            # Vibraslap → snare
    60: 50,  61: 47,   # Hi/lo bongo → toms
    62: 45,  63: 43, 64: 41,  # Congas → toms
}

# ── Standard tunings (MIDI note, string 1 = highest pitch) ───────────────────
GUITAR_STD = [64, 59, 55, 50, 45, 40]   # E4 B3 G3 D3 A2 E2
BASS_4_STD  = [43, 38, 33, 28]           # G2 D2 A1 E1


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class NoteEvent:
    tick:     int   # absolute MIDI tick
    pitch:    int   # MIDI note number
    velocity: int   # 1-127
    channel:  int   # MIDI channel


# ── MIDI parsing ──────────────────────────────────────────────────────────────

def _parse_midi(path: str | Path):
    """
    Returns (ppq, tempo_map, time_sig_map, tracks).
      tempo_map   : [(tick, us_per_beat), …]
      time_sig_map: [(tick, numerator, denominator), …]
      tracks      : [(name, primary_channel, [NoteEvent, …]), …]
    """
    mid = mido.MidiFile(str(path))
    ppq = mid.ticks_per_beat

    tempo_map    = []
    time_sig_map = [(0, 4, 4)]

    # Conductor / tempo track
    abs_tick = 0
    for msg in mid.tracks[0]:
        abs_tick += msg.time
        if msg.type == 'set_tempo':
            tempo_map.append((abs_tick, msg.tempo))
        elif msg.type == 'time_signature':
            time_sig_map.append((abs_tick, msg.numerator, msg.denominator))

    # Instrument tracks
    tracks_out = []
    track_list = mid.tracks[1:] if mid.type == 1 else [mid.tracks[0]]
    for midi_track in track_list:
        if not midi_track:
            continue

        abs_tick = 0
        pending:  dict[tuple, tuple] = {}  # (pitch, ch) → (tick, vel)
        events:   list[NoteEvent]    = []
        channels: set[int]           = set()

        for msg in midi_track:
            abs_tick += msg.time
            if not hasattr(msg, 'channel'):
                continue
            ch = msg.channel
            channels.add(ch)

            if msg.type == 'note_on' and msg.velocity > 0:
                pending[(msg.note, ch)] = (abs_tick, msg.velocity)
            elif msg.type in ('note_on', 'note_off'):
                info = pending.pop((msg.note, ch), None)
                if info:
                    start_tick, vel = info
                    events.append(NoteEvent(start_tick, msg.note, vel, ch))

        if events:
            name = midi_track.name.strip() or f"Track {len(tracks_out)+1}"
            primary_ch = min(channels) if channels else 0
            tracks_out.append((name, primary_ch,
                                sorted(events, key=lambda e: e.tick)))

    return ppq, tempo_map, time_sig_map, tracks_out


# ── Feel detection ────────────────────────────────────────────────────────────

def _dist_to_grid(pos: float, grid: float) -> float:
    """Minimum distance from pos to the nearest multiple of grid."""
    r = pos % grid
    return min(r, grid - r)


def _detect_feel(events: list[NoteEvent], ppq: int) -> str:
    """
    Classify a track as 'triplet8' or 'straight'.

    Uses two signals combined:
      1. Position alignment: how many non-beat notes sit closer to the
         triplet-8th (ppq/3) vs straight-16th (ppq/4) grid.
      2. Inter-note gaps: spacing between consecutive distinct ticks,
         which is more robust to grace notes / flams that skew positions.

    'triplet8' wins when combined triplet score exceeds straight score.
    """
    if len(events) < 4:
        return 'straight'

    grid_t = ppq / 3   # 320 at ppq=960
    grid_s = ppq / 4   # 240 at ppq=960

    # Signal 1: position within beat (skip exact beat boundaries — ambiguous)
    t_pos = s_pos = 0
    for ev in events:
        pos = ev.tick % ppq
        if pos == 0:
            continue
        d_t = _dist_to_grid(pos, grid_t)
        d_s = _dist_to_grid(pos, grid_s)
        if d_t < d_s:
            t_pos += 1
        elif d_s < d_t:
            s_pos += 1

    # Signal 2: inter-note gaps (robust to timing noise)
    ticks = sorted(set(e.tick for e in events))
    t_gap = s_gap = 0
    for i in range(len(ticks) - 1):
        g = ticks[i + 1] - ticks[i]
        if g == 0:
            continue
        d_t = _dist_to_grid(g, grid_t)
        d_s = _dist_to_grid(g, grid_s)
        if d_t < d_s:
            t_gap += 1
        elif d_s < d_t:
            s_gap += 1

    return 'triplet8' if (t_pos + t_gap) > (s_pos + s_gap) else 'straight'


# ── Tuning helpers ────────────────────────────────────────────────────────────

def _tuning_for(events: list[NoteEvent]) -> list[int]:
    """Guess instrument tuning from note range."""
    pitches = [e.pitch for e in events]
    return BASS_4_STD if pitches and max(pitches) < 55 else GUITAR_STD


def _assign_sfret(pitch: int, tuning: list[int],
                  prev_str: int = 1) -> tuple[int, int]:
    """Return (string_1indexed, fret) via greedy cost minimisation."""
    best, best_cost = None, 1e9
    for i, open_p in enumerate(tuning):
        fret = pitch - open_p
        if 0 <= fret <= MAX_FRET:
            cost = fret + abs((i + 1) - prev_str) * 2
            if cost < best_cost:
                best_cost = cost
                best = (i + 1, fret)
    if best is None:
        best = (1, max(0, min(MAX_FRET, pitch - tuning[0])))
    return best


# ── Velocity mapping ──────────────────────────────────────────────────────────

def _gp_vel(v: int) -> int:
    for band in (15, 31, 47, 63, 79, 95, 111, 127):
        if v <= band:
            return band
    return 127


# ── Measure boundary calculation ──────────────────────────────────────────────

def _measure_ticks(num: int, den: int, ppq: int) -> int:
    return int(ppq * 4 * num / den)


def _build_measures(ppq: int, time_sig_map: list, max_tick: int) -> list:
    """Return [(midi_tick_start, numerator, denominator), …] covering all notes."""
    measures = []
    tick = 0
    num, den = 4, 4
    while tick <= max_tick + _measure_ticks(num, den, ppq):
        # Resolve time signature at current tick
        for (ts_tick, ts_num, ts_den) in time_sig_map:
            if ts_tick <= tick:
                num, den = ts_num, ts_den

        measures.append((tick, num, den))
        tick += _measure_ticks(num, den, ppq)

        if len(measures) > 999:  # safety cap
            break
    return measures


# ── GP5 assembly ──────────────────────────────────────────────────────────────

def convert(midi_path: str | Path, output_path: str | Path,
            notation: str = 'standard') -> list[str]:
    """
    Convert *midi_path* to a Guitar Pro 5 file at *output_path*.
    Returns a list of non-fatal warning strings (empty on clean conversion).

    notation: 'standard' (score only), 'tab' (tablature only), 'both'
    """
    warnings: list[str] = []

    ppq, tempo_map, time_sig_map, tracks = _parse_midi(midi_path)

    if not tracks:
        raise ValueError("No instrument tracks found in MIDI file.")

    max_tick = max(e.tick for _, _, evs in tracks for e in evs)
    measures_info = _build_measures(ppq, time_sig_map, max_tick)

    # ── Song skeleton ──────────────────────────────────────────────────────────
    song = gp.Song()
    song.title = Path(midi_path).stem
    # Tempo in effect at tick 0: last event at tick≤0, default 120 BPM
    initial_us = 500_000
    for tick, us in tempo_map:
        if tick <= 0:
            initial_us = us
    song.tempo = max(1, round(60_000_000 / initial_us))
    song.measureHeaders.clear()
    song.tracks.clear()

    # Measure headers (shared across all tracks)
    for i, (m_tick, m_num, m_den) in enumerate(measures_info):
        gp_start = QUARTER + int(m_tick * QUARTER / ppq)
        mh = gp.MeasureHeader(number=i + 1, start=gp_start)
        mh.timeSignature.numerator = m_num
        mh.timeSignature.denominator.value = m_den
        song.measureHeaders.append(mh)

    # ── One GP track per MIDI track ────────────────────────────────────────────
    for t_idx, (t_name, t_ch, events) in enumerate(tracks):
        is_drum = (t_ch == 9)
        feel    = _detect_feel(events, ppq)

        # Grid parameters
        if feel == 'triplet8':
            slot_midi = ppq / 3          # 320 at ppq=960
            slot_gp   = QUARTER // 3     # 320 ticks in GP units
            dur_val   = gp.Duration.eighth
            use_tuplet = True
        else:
            slot_midi = ppq / 4          # 240 at ppq=960
            slot_gp   = QUARTER // 4     # 240 ticks
            dur_val   = gp.Duration.sixteenth
            use_tuplet = False

        # ── String definitions ─────────────────────────────────────────────────
        if is_drum:
            # GP5 supports max 7 strings. Pick the 7 most-used pitches.
            from collections import Counter
            gp_pitch_counts = Counter(GM_TO_GP.get(e.pitch, e.pitch) for e in events)
            # Sort by frequency desc, then pitch desc for determinism
            top_pitches = [p for p, _ in sorted(
                gp_pitch_counts.items(), key=lambda x: (-x[1], -x[0])
            )][:7]
            unique_p = sorted(top_pitches, reverse=True)  # high→low for display

            gp_strings  = [gp.GuitarString(i + 1, p) for i, p in enumerate(unique_p)]
            pitch_to_str = {p: i + 1 for i, p in enumerate(unique_p)}
            # Map overflow pitches to nearest pitch in set
            def _nearest_str(raw_pitch: int) -> int:
                gp_p = GM_TO_GP.get(raw_pitch, raw_pitch)
                if gp_p in pitch_to_str:
                    return pitch_to_str[gp_p]
                nearest = min(unique_p, key=lambda x: abs(x - gp_p))
                return pitch_to_str[nearest]
            tuning       = None
        else:
            tuning      = _tuning_for(events)
            gp_strings  = [gp.GuitarString(i + 1, p) for i, p in enumerate(tuning)]
            pitch_to_str = None

        # ── Track object ───────────────────────────────────────────────────────
        track = gp.Track(
            song,
            number=t_idx + 1,
            fretCount=24,
            isPercussionTrack=is_drum,
            name=t_name[:40],
            strings=gp_strings,
        )
        track.channel.channel       = t_ch
        track.channel.effectChannel = t_ch
        track.channel.instrument    = 0 if is_drum else 25
        track.settings.notation  = notation in ('standard', 'both')
        track.settings.tablature = notation in ('tab', 'both')
        song.tracks.append(track)
        # track.measures is auto-populated with one empty Measure per header

        # ── Fill measures ──────────────────────────────────────────────────────
        prev_str = 1

        for m_idx, (m_tick, m_num, m_den) in enumerate(measures_info):
            m_len  = _measure_ticks(m_num, m_den, ppq)
            m_end  = m_tick + m_len
            m_evs  = [e for e in events if m_tick <= e.tick < m_end]

            measure  = track.measures[m_idx]
            voice    = measure.voices[0]
            gp_start = song.measureHeaders[m_idx].start
            num_slots = max(1, int(round(m_len / slot_midi)))

            # Quantize: tick → slot index
            slot_notes: dict[int, list[NoteEvent]] = {}
            for ev in m_evs:
                rel = ev.tick - m_tick
                slot = min(int(round(rel / slot_midi)), num_slots - 1)
                slot_notes.setdefault(slot, []).append(ev)

            # Build beats slot by slot
            for slot in range(num_slots):
                gp_tick = gp_start + slot * slot_gp

                d = gp.Duration(value=dur_val)
                if use_tuplet:
                    d.tuplet = gp.Tuplet(enters=3, times=2)

                if slot in slot_notes:
                    beat = gp.Beat(voice, start=gp_tick)
                    beat.duration = d
                    beat.status   = gp.BeatStatus.normal

                    gp_notes: list[gp.Note] = []
                    used_strings: set[int]  = set()
                    used_gp_pitches: dict[int, int] = {}  # gp_pitch → velocity

                    for ev in slot_notes[slot]:
                        if is_drum:
                            gp_pitch = GM_TO_GP.get(ev.pitch, ev.pitch)
                            # Keep only the hardest hit when two GM notes share
                            # the same GP pitch (e.g. MIDI 35 & 36 → GP 36)
                            if gp_pitch in used_gp_pitches:
                                if ev.velocity <= used_gp_pitches[gp_pitch]:
                                    continue   # keep the louder hit already recorded
                                # louder hit — remove the earlier note and re-add
                                gp_notes = [n for n in gp_notes if n.value != gp_pitch]
                                used_strings.discard(
                                    next((n.string for n in gp_notes
                                          if n.value == gp_pitch), None) or 0)
                            used_gp_pitches[gp_pitch] = ev.velocity
                            str_num = _nearest_str(ev.pitch)
                            # Resolve string collision: cycle to a free string
                            for _ in range(len(unique_p)):
                                if str_num not in used_strings:
                                    break
                                str_num = str_num % len(unique_p) + 1
                            else:
                                continue  # all strings occupied — drop this hit
                            used_strings.add(str_num)
                            gp_notes.append(gp.Note(
                                beat,
                                value=gp_pitch,
                                velocity=_gp_vel(ev.velocity),
                                string=str_num,
                                type=gp.NoteType.normal,
                            ))
                        else:
                            s, fret = _assign_sfret(ev.pitch, tuning, prev_str)
                            # Resolve string collision
                            if s in used_strings:
                                # Try neighbouring strings, recomputing fret
                                found = False
                                for try_s in range(1, len(tuning) + 1):
                                    if try_s not in used_strings:
                                        fret = max(0, min(MAX_FRET,
                                                          ev.pitch - tuning[try_s - 1]))
                                        s = try_s
                                        found = True
                                        break
                                if not found:
                                    continue  # all strings occupied — drop this note
                            used_strings.add(s)
                            prev_str = s
                            gp_notes.append(gp.Note(
                                beat,
                                value=fret,
                                velocity=_gp_vel(ev.velocity),
                                string=s,
                                type=gp.NoteType.normal,
                            ))

                    beat.notes = gp_notes
                    voice.beats.append(beat)

                else:  # rest
                    beat = gp.Beat(voice, start=gp_tick)
                    beat.duration = d
                    beat.status   = gp.BeatStatus.rest
                    voice.beats.append(beat)

            # Guitar Pro requires every voice to contain beats (not 0).
            # Fill voice[1] with a rest that spans the full measure so the
            # file opens correctly in Guitar Pro 5–8.
            voice2 = measure.voices[1]
            if not voice2.beats:
                rest = gp.Beat(voice2, start=gp_start)
                rest.status = gp.BeatStatus.rest
                # Pick the largest standard duration that fits the measure.
                # For most songs (4/4) a whole note fits exactly.
                rest.duration = gp.Duration(value=gp.Duration.whole)
                voice2.beats.append(rest)

    # ── Write & validate ───────────────────────────────────────────────────────
    gp.write(song, str(output_path))

    try:
        gp.parse(str(output_path))
    except Exception as exc:
        warnings.append(f"Post-write validation warning: {exc}")

    return warnings
