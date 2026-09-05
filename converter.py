"""Convert Songsterr track data to a MIDI file."""

import math
import mido

PPQ = 960  # ticks per quarter note — high resolution for unusual time sigs

DRUM_INSTRUMENT_ID = 1024


def _duration_ticks(duration: list[int]) -> int:
    num, den = duration
    return int(round((num / den) * 4 * PPQ))


def _tempo_us(bpm: float) -> int:
    return int(round(60_000_000 / bpm))


def _measure_ticks(sig: tuple[int, int]) -> int:
    num, den = sig
    return int(round((num / den) * 4 * PPQ))


def _measure_start_ticks(measures: list[dict]) -> list[int]:
    starts = []
    tick = 0
    sig = (4, 4)
    for m in measures:
        starts.append(tick)
        if "signature" in m:
            sig = tuple(m["signature"])
        tick += _measure_ticks(sig)
    return starts


def _build_tempo_track(reference_track_data: dict) -> mido.MidiTrack:
    """Build MIDI track 0: tempo + time-signature meta events."""
    track = mido.MidiTrack()
    measures = reference_track_data.get("measures", [])
    autos = reference_track_data.get("automations") or {}

    measure_starts = _measure_start_ticks(measures)

    meta_events: list[tuple[int, mido.MetaMessage]] = []

    for t in autos.get("tempo", []):
        m_idx = t["measure"]
        if m_idx >= len(measure_starts):
            continue
        tick = measure_starts[m_idx] + t.get("position", 0)
        meta_events.append((tick, mido.MetaMessage("set_tempo", tempo=_tempo_us(t["bpm"]), time=0)))

    if not autos.get("tempo"):
        meta_events.append((0, mido.MetaMessage("set_tempo", tempo=_tempo_us(120), time=0)))

    current_tick = 0
    current_sig = (4, 4)
    had_sig = False
    for m in measures:
        if "signature" in m:
            num, den = m["signature"]
            den_pow2 = 2 ** round(math.log2(den)) if den > 0 else 4
            meta_events.append((current_tick, mido.MetaMessage(
                "time_signature", numerator=num, denominator=den_pow2, time=0
            )))
            current_sig = (num, den)
            had_sig = True
        current_tick += _measure_ticks(current_sig)

    if not had_sig:
        meta_events.append((0, mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0)))

    meta_events.sort(key=lambda x: x[0])
    prev = 0
    for abs_tick, msg in meta_events:
        msg.time = abs_tick - prev
        track.append(msg)
        prev = abs_tick

    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def _build_drum_track(track_data: dict) -> mido.MidiTrack:
    track = mido.MidiTrack()
    track.name = track_data.get("name", "Drums").encode("latin-1", errors="replace").decode("latin-1")

    measures = track_data.get("measures", [])
    measure_starts = _measure_start_ticks(measures)

    events: list[tuple[int, mido.Message]] = []

    for m_idx, measure in enumerate(measures):
        measure_start = measure_starts[m_idx]
        for voice in measure.get("voices", []):
            beat_tick = measure_start
            for beat in voice.get("beats", []):
                dur = _duration_ticks(beat["duration"])
                if not beat.get("rest"):
                    velocity = _velocity(beat.get("velocity", "mf"))
                    for note in beat.get("notes", []):
                        if note.get("rest") or "fret" not in note:
                            continue
                        midi_pitch = note["fret"]
                        if not note.get("tie"):
                            events.append((beat_tick, mido.Message(
                                "note_on", channel=9, note=midi_pitch,
                                velocity=velocity, time=0
                            )))
                        events.append((beat_tick + dur - 1, mido.Message(
                            "note_off", channel=9, note=midi_pitch,
                            velocity=0, time=0
                        )))
                beat_tick += dur

    events.sort(key=lambda x: x[0])
    prev = 0
    for abs_tick, msg in events:
        track.append(msg.copy(time=abs_tick - prev))
        prev = abs_tick

    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


def _build_pitched_track(track_data: dict, channel: int) -> mido.MidiTrack:
    track = mido.MidiTrack()
    track.name = track_data.get("name", "Track").encode("latin-1", errors="replace").decode("latin-1")

    instrument_id = track_data.get("instrumentId", 0)
    program = instrument_id if 0 <= instrument_id <= 127 else 0
    track.append(mido.Message("program_change", channel=channel, program=program, time=0))

    # Open-string tuning: MIDI note for each string at fret 0.
    # Songsterr may return "strings" as an int (count) — fall back to "tuning" list or empty.
    strings = track_data.get("tuning") or track_data.get("strings", [])
    if not isinstance(strings, list):
        strings = []

    measures = track_data.get("measures", [])
    measure_starts = _measure_start_ticks(measures)

    events: list[tuple[int, mido.Message]] = []

    for m_idx, measure in enumerate(measures):
        measure_start = measure_starts[m_idx]
        for voice in measure.get("voices", []):
            beat_tick = measure_start
            for beat in voice.get("beats", []):
                dur = _duration_ticks(beat["duration"])
                if not beat.get("rest"):
                    velocity = _velocity(beat.get("velocity", "mf"))
                    for note in beat.get("notes", []):
                        if note.get("rest"):
                            continue
                        fret = note.get("fret")
                        if fret is None:
                            continue
                        string_idx = note.get("string", 0)
                        if strings and string_idx < len(strings):
                            midi_pitch = strings[string_idx] + fret
                        else:
                            midi_pitch = fret
                        midi_pitch = max(0, min(127, midi_pitch))
                        if not note.get("tie"):
                            events.append((beat_tick, mido.Message(
                                "note_on", channel=channel, note=midi_pitch,
                                velocity=velocity, time=0
                            )))
                        events.append((beat_tick + dur - 1, mido.Message(
                            "note_off", channel=channel, note=midi_pitch,
                            velocity=0, time=0
                        )))
                beat_tick += dur

    events.sort(key=lambda x: x[0])
    prev = 0
    for abs_tick, msg in events:
        track.append(msg.copy(time=abs_tick - prev))
        prev = abs_tick

    track.append(mido.MetaMessage("end_of_track", time=0))
    return track


_VELOCITY_MAP = {
    "ppp": 16, "pp": 32, "p": 48, "mp": 64,
    "mf": 80, "f": 96, "ff": 112, "fff": 127,
}


def _velocity(v: str) -> int:
    return _VELOCITY_MAP.get(v, 80)


def convert(revision: dict, all_track_data: list[dict]) -> mido.MidiFile:
    """Convert all tracks from a Songsterr revision to a multi-track MidiFile."""
    if not all_track_data:
        raise ValueError("No tracks found in this revision.")

    mid = mido.MidiFile(type=1, ticks_per_beat=PPQ)
    mid.tracks.append(_build_tempo_track(all_track_data[0]))

    channel = 0
    for td in all_track_data:
        if td.get("instrumentId") == DRUM_INSTRUMENT_ID:
            mid.tracks.append(_build_drum_track(td))
        else:
            if channel == 9:
                channel += 1
            mid.tracks.append(_build_pitched_track(td, channel))
            channel += 1
            if channel > 15:
                channel = 15

    return mid
