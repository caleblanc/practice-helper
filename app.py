#!/usr/bin/env python3
"""Practice Helper — streaming audio, stem separation, and Songsterr scores."""

__version__ = "0.02"

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import shlex
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
import httpx
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import songsterr as sapi
import providers
import converter as conv
import midi_to_gp
import songsterr_to_gp
import gp_audio

# ── Platform helpers ──────────────────────────────────────────────────────────
_WIN  = sys.platform == "win32"
_MAC  = sys.platform == "darwin"
_VBIN = "Scripts" if _WIN else "bin"        # venv bin directory name
_EXE  = ".exe"  if _WIN else ""            # executable suffix

# ── Paths ─────────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).parent
CONFIG  = APP_DIR / "config.json"


def _config_dir() -> Path:
    """Per-user config location, so an installed copy stays writable."""
    if _WIN:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif _MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "PracticeHelper"


# Prefer a config.json sitting next to app.py (how a cloned repo behaves), and
# fall back to the per-user location for an installed/packaged copy.
if not CONFIG.exists():
    _user_cfg = _config_dir() / "config.json"
    if _user_cfg.exists() or not os.access(APP_DIR, os.W_OK):
        _user_cfg.parent.mkdir(parents=True, exist_ok=True)
        CONFIG = _user_cfg


def find_tool(name: str, extra: str = "") -> str:
    """Locate a helper executable without assuming where it was installed.

    Checked in order: an explicit override from Settings, the venv running this
    app, a .venv beside app.py, then PATH. Returns "" when it isn't found, so
    callers can report a useful message instead of an opaque OSError.
    """
    exe = f"{name}{_EXE}"
    roots = []
    if extra:
        roots.append(Path(extra).expanduser())
    roots.append(Path(sys.prefix))
    roots.append(APP_DIR / ".venv")
    for root in roots:
        for cand in (root / _VBIN / exe, root / exe):
            if cand.is_file():
                return str(cand)
    return shutil.which(name) or ""

DRUM_INSTRUMENT_ID = 1024



def _instrument_category(instrument_id: int) -> tuple[str, str]:
    """Returns (category_name, emoji) for a Songsterr instrumentId."""
    if instrument_id == 1024:
        return ("Drums", "🥁")
    if 24 <= instrument_id <= 31:
        return ("Guitar", "🎸")
    if 32 <= instrument_id <= 39:
        return ("Bass", "🎸")
    if 0 <= instrument_id <= 7:
        return ("Piano", "🎹")
    if 8 <= instrument_id <= 23:
        return ("Keys", "🎹")
    if 40 <= instrument_id <= 55:
        return ("Strings", "🎻")
    if 56 <= instrument_id <= 63:
        return ("Brass", "🎺")
    if 64 <= instrument_id <= 71:
        return ("Reed", "🎷")
    if 72 <= instrument_id <= 79:
        return ("Pipe", "🪈")
    if 80 <= instrument_id <= 127:
        return ("Synth", "🎹")
    return ("Other", "🎵")
DEFAULT_OUTPUT  = str(Path.home() / "Music" / "Practice Helper" / "Downloads")
DEFAULT_STEMS   = str(Path.home() / "Music" / "Practice Helper" / "Songs")

# ── Theme ─────────────────────────────────────────────────────────────────────
BG       = "#0c0c0e"
SURFACE  = "#141416"
SURFACE2 = "#1c1c1f"
BORDER   = "#2a2a2e"
RED      = "#fc3c44"
RED_DIM  = "#c02b32"
MUTED    = "#888888"
GREEN    = "#30d158"
BLUE     = "#4ca8f0"
PURPLE   = "#bf5af2"
ORANGE   = "#ff9f0a"

# Sources each demucs model can separate.
STEM_SOURCES = {
    "htdemucs":    ["drums", "bass", "other", "vocals"],
    "htdemucs_6s": ["drums", "bass", "other", "vocals", "guitar", "piano"],
}
STEM_MODEL_LABELS = {
    "htdemucs":    "4-source (faster)",
    "htdemucs_6s": "6-source (adds guitar & piano)",
}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


# ── Trackpad scrolling ────────────────────────────────────────────────────────
# Tk 9 (which Python 3.14 ships) routes precision-trackpad gestures to
# <TouchpadScroll> instead of <MouseWheel>. CustomTkinter 5.2.2 predates Tk 9 and
# binds only <MouseWheel>, so a mouse wheel scrolls its frames but a trackpad
# does nothing at all. Teach every CTkScrollableFrame the new event, reusing
# CTk's own hit-test so the frame under the pointer is the one that moves.
#
# The delta is packed: deltaX in the high bits, deltaY in the low 16 (signed).
# tk::PreciseScrollDeltas unpacks it, and the fraction arithmetic mirrors Tk's
# own tk::ScrollByPixels so the direction matches every other app.

def _owns_widget(frame, widget):
    """Is *widget* inside this scrollable frame?

    CustomTkinter's own hit-test is private and has already been renamed once
    (check_if_master_is_canvas in 5.x, _check_if_valid_scroll in 6.x), so use
    whichever exists and otherwise walk the master chain ourselves rather than
    silently losing the binding on the next rename.
    """
    for name in ("check_if_master_is_canvas", "_check_if_valid_scroll"):
        fn = getattr(frame, name, None)
        if fn is not None:
            try:
                return bool(fn(widget))
            except Exception:
                break
    canvas = frame._parent_canvas
    while widget is not None:
        if widget is canvas or widget is frame:
            return True
        widget = getattr(widget, "master", None)
    return False


def _touchpad_scroll(self, event):
    try:
        if not _owns_widget(self, event.widget):
            return
        canvas = self._parent_canvas
        dx, dy = canvas.tk.call("tk::PreciseScrollDeltas", event.delta)
        if getattr(self, "_shift_pressed", False) and dy and not dx:
            dx, dy = dy, 0
        if dx and canvas.xview() != (0.0, 1.0):
            canvas.xview_moveto(canvas.xview()[0] - dx / max(canvas.winfo_width(), 1))
        if dy and canvas.yview() != (0.0, 1.0):
            canvas.yview_moveto(canvas.yview()[0] - dy / max(canvas.winfo_height(), 1))
    except Exception:
        pass


if not hasattr(ctk.CTkScrollableFrame, "_touchpad_patched"):
    ctk.CTkScrollableFrame._touchpad_scroll = _touchpad_scroll
    ctk.CTkScrollableFrame._touchpad_patched = True
    _ctk_sf_init = ctk.CTkScrollableFrame.__init__

    def _sf_init(self, *a, **kw):
        _ctk_sf_init(self, *a, **kw)
        self.bind_all("<TouchpadScroll>", self._touchpad_scroll, add="+")

    ctk.CTkScrollableFrame.__init__ = _sf_init


# ── Click binding helper ───────────────────────────────────────────────────────
# Tkinter events don't propagate from child widgets to parent frames automatically.
# This binds the callback on every descendant so clicking anywhere in a row fires it.

def _bind_click(widget, callback):
    widget.bind("<Button-1>", callback, add=True)
    for attr in ("_canvas", "_fg_frame", "_label"):
        internal = getattr(widget, attr, None)
        if internal is not None:
            internal.bind("<Button-1>", callback, add=True)
    for child in widget.winfo_children():
        _bind_click(child, callback)


# ── Config ────────────────────────────────────────────────────────────────────

def load_cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def save_cfg(cfg: dict):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def sanitize(name: str) -> str:
    return re.sub(r'[^\w\s\-.]', '', name).strip()


def scan_m4a(output_dir: str) -> set:
    try:
        return set(Path(output_dir).rglob("*.m4a"))
    except Exception:
        return set()


def find_new_m4a(before: set, output_dir: str) -> Path | None:
    after = scan_m4a(output_dir)
    new = after - before
    if not new:
        return None
    return max(new, key=lambda p: p.stat().st_mtime)


def find_existing_m4a(output_dir: str, track_name: str,
                      artist: str | None = None) -> Path | None:
    """Return the library .m4a for this track, exact title match preferred.

    A bare substring match is far too loose -- "Bleed" happily matches
    "Bleeding Mascara" -- so an exact hit on the title (after dropping gamdl's
    leading track number) always wins over a containment hit.
    """
    needle = sanitize(track_name).lower()
    if not needle:
        return None
    art = sanitize(artist or "").lower()
    exact = loose = None
    try:
        for p in Path(output_dir).rglob("*.m4a"):
            # sanitize BOTH sides: the needle has had punctuation stripped, so an
            # apostrophe in the filename ("Don't Run") would never match otherwise.
            stem = sanitize(re.sub(r"^\d+\s+", "", p.stem)).lower()
            if stem == needle:
                if not art or art in str(p.parent).lower():
                    return p
                exact = exact or p
            elif needle in stem and loose is None:
                # A containment hit is only trustworthy inside the right artist
                # folder; otherwise "Bleed" silently becomes "Bleeding Mascara".
                if not art or art in str(p.parent).lower():
                    loose = p
    except Exception:
        pass
    return exact or loose


# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(ctk.CTkToplevel):
    _NOTATION_LABELS = {"standard": "Standard notation", "tab": "TAB only", "both": "Both"}
    _NOTATION_VALUES = {v: k for k, v in _NOTATION_LABELS.items()}

    def __init__(self, parent: "App", focus_provider: bool = False):
        super().__init__(parent)
        self.app = parent
        self.title("Settings")
        self.geometry("560x620")
        self.minsize(460, 400)
        self.grab_set()
        self._entries: dict[str, ctk.CTkEntry] = {}
        self._focus_provider = focus_provider
        self._build()

    # ── field helpers ─────────────────────────────────────────────────────────

    def _row(self, parent, label: str, key: str, value: str,
             kind: str = "text", help_text: str = ""):
        ctk.CTkLabel(parent, text=label, text_color=MUTED, anchor="w",
                     font=("", 12)).pack(fill="x", padx=4, pady=(10, 2))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=4)
        entry = ctk.CTkEntry(row, height=32, show="\u2022" if kind == "secret" else "")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        entry.insert(0, value or "")
        self._entries[key] = entry

        if kind in ("file", "dir"):
            def browse(entry=entry, kind=kind):
                p = (filedialog.askdirectory() if kind == "dir"
                     else filedialog.askopenfilename(
                         filetypes=[("Text", "*.txt"), ("All", "*.*")]))
                if p:
                    entry.delete(0, "end")
                    entry.insert(0, p)
            ctk.CTkButton(row, text="Browse", width=72, height=32,
                          fg_color=SURFACE2, hover_color=BORDER,
                          command=browse).pack(side="left")
        if help_text:
            ctk.CTkLabel(parent, text=help_text, text_color=MUTED, anchor="w",
                         font=("", 10), wraplength=470).pack(fill="x", padx=6, pady=(2, 0))
        return entry

    def _heading(self, parent, text, colour=None):
        ctk.CTkLabel(parent, text=text, text_color=colour or MUTED, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(fill="x", padx=4, pady=(18, 0))

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self):
        prov = self.app.provider()
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(12, 0))

        # ── Streaming service ────────────────────────────────────────────────
        self._heading(body, "Streaming service", prov.accent)
        self._prov_seg = ctk.CTkSegmentedButton(
            body, values=[providers.get(x).name for x in providers.ORDER],
            font=("", 11), selected_color=prov.accent,
            selected_hover_color=prov.dim, command=self._on_provider_change)
        self._prov_seg.set(prov.name)
        self._prov_seg.pack(fill="x", padx=4, pady=(6, 0))

        # Credentials are per-service, so this block is rebuilt on every switch.
        self._prov_box = ctk.CTkFrame(body, fg_color="transparent")
        self._prov_box.pack(fill="x")
        self._build_provider_fields()

        # ── Folders ──────────────────────────────────────────────────────────
        self._heading(body, "Folders")
        self._row(body, "Downloaded audio", "output_path",
                  self.app.output_path, "dir")
        self._row(body, "Songs, stems & scores", "stems_path",
                  self.app.stems_path, "dir")
        self._row(body, "Helper tools folder (optional)", "tools_path",
                  self.app.tools_path, "dir",
                  "Where gamdl / demucs / ffmpeg live, if they are not on your PATH.")

        # ── Output ───────────────────────────────────────────────────────────
        self._heading(body, "Output")
        ctk.CTkLabel(body, text="Guitar Pro notation display", text_color=MUTED,
                     anchor="w", font=("", 12)).pack(fill="x", padx=4, pady=(8, 4))
        self._notation_seg = ctk.CTkSegmentedButton(
            body, values=list(self._NOTATION_LABELS.values()), font=("", 12))
        self._notation_seg.set(self._NOTATION_LABELS.get(self.app.notation_mode,
                                                         "Standard notation"))
        self._notation_seg.pack(fill="x", padx=4)

        ctk.CTkLabel(body, text="Songsterr output formats", text_color=MUTED,
                     anchor="w", font=("", 12)).pack(fill="x", padx=4, pady=(14, 4))
        fmt = ctk.CTkFrame(body, fg_color="transparent")
        fmt.pack(fill="x", padx=4)
        self._var_midi = ctk.BooleanVar(value=bool(getattr(self.app, "want_midi", True)))
        self._var_gp   = ctk.BooleanVar(value=bool(getattr(self.app, "want_gp", True)))
        ctk.CTkCheckBox(fmt, text="MIDI", variable=self._var_midi,
                        font=("", 12), width=90).pack(side="left", padx=(0, 8))
        ctk.CTkCheckBox(fmt, text="Guitar Pro", variable=self._var_gp,
                        font=("", 12), width=110).pack(side="left")

        ctk.CTkLabel(body, text="Stem separation model", text_color=MUTED,
                     anchor="w", font=("", 12)).pack(fill="x", padx=4, pady=(14, 4))
        self._model_seg = ctk.CTkSegmentedButton(
            body, values=list(STEM_MODEL_LABELS.values()), font=("", 12))
        self._model_seg.set(STEM_MODEL_LABELS.get(self.app.demucs_model,
                                                  STEM_MODEL_LABELS["htdemucs"]))
        self._model_seg.pack(fill="x", padx=4, pady=(0, 12))

        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", pady=12)
        self._save_btn = ctk.CTkButton(foot, text="Save", width=110, height=34,
                                       fg_color=prov.accent, hover_color=prov.dim,
                                       command=self._save)
        self._save_btn.pack()

    def _build_provider_fields(self):
        """Credential inputs + download command for the selected service."""
        for w in self._prov_box.winfo_children():
            w.destroy()
        prov = providers.get(self._pending_pid())

        if prov.note:
            ctk.CTkLabel(self._prov_box, text=prov.note, text_color=MUTED,
                         anchor="w", font=("", 10), wraplength=470
                         ).pack(fill="x", padx=6, pady=(8, 0))

        for f in prov.fields:
            self._row(self._prov_box, f.label, f"cred:{f.key}",
                      self.app.creds.get(f.key, ""), f.kind, f.help)

        current = self.app.download_cmds.get(prov.id, prov.download_cmd)
        self._row(self._prov_box, "Download command", f"cmd:{prov.id}", current, "text",
                  "Leave blank to search only. {url} and {out} are filled in, along "
                  "with the fields above (e.g. {%s})."
                  % (prov.fields[0].key if prov.fields else "url"))

    def _pending_pid(self) -> str:
        name = self._prov_seg.get()
        for pid in providers.ORDER:
            if providers.get(pid).name == name:
                return pid
        return self.app.provider_id

    def _on_provider_change(self, _name):
        # Keep whatever the user has already typed for the outgoing service.
        self._stash_provider_fields()
        prov = providers.get(self._pending_pid())
        self._save_btn.configure(fg_color=prov.accent, hover_color=prov.dim)
        self._prov_seg.configure(selected_color=prov.accent,
                                 selected_hover_color=prov.dim)
        self._build_provider_fields()

    def _stash_provider_fields(self):
        for key, entry in list(self._entries.items()):
            if key.startswith("cred:"):
                self.app.creds[key[5:]] = entry.get().strip()
            elif key.startswith("cmd:"):
                self.app.download_cmds[key[4:]] = entry.get().strip()
        self._entries = {k: v for k, v in self._entries.items()
                         if not k.startswith(("cred:", "cmd:"))}

    def _save(self):
        self._stash_provider_fields()
        self.app.provider_id    = self._pending_pid()
        self.app.output_path    = self._entries["output_path"].get().strip() or DEFAULT_OUTPUT
        self.app.stems_path     = self._entries["stems_path"].get().strip() or DEFAULT_STEMS
        self.app.tools_path     = self._entries["tools_path"].get().strip()
        self.app.notation_mode  = self._NOTATION_VALUES.get(
            self._notation_seg.get(), "standard")
        _mv = {v: k for k, v in STEM_MODEL_LABELS.items()}
        self.app.demucs_model = _mv.get(self._model_seg.get(), "htdemucs")
        self.app.want_midi    = bool(self._var_midi.get())
        self.app.want_gp      = bool(self._var_gp.get())
        self.app.persist()
        if hasattr(self.app, "am_panel"):
            self.app.am_panel.retheme()
        if hasattr(self.app, "ss_panel"):
            self.app.ss_panel.refresh_audio_options()
            self.app.ss_panel.refresh_header()
        if hasattr(self.app, "_retheme"):
            self.app._retheme()
        self.destroy()


GLASS   = "#101013"      # SURFACE blended toward BG - fakes translucency
SS_TINT = "#12283f"      # Guitar Pro blue (Songsterr side is fixed)
COL_W, COL_MIN = 178, 40


class CollapsibleColumn(ctk.CTkFrame):
    """Narrow vertical options column that starts collapsed."""

    def __init__(self, parent, accent, tint, title):
        super().__init__(parent, fg_color=tint, corner_radius=8,
                         border_width=1, border_color=accent, width=COL_MIN)
        self.pack_propagate(False)
        self._accent, self._title, self._open = accent, title, False
        self._tab = ctk.CTkButton(self, text="\u25b6", width=COL_MIN - 8, height=32,
                                  fg_color="transparent", hover_color=accent,
                                  text_color=accent,
                                  font=ctk.CTkFont(size=18, weight="bold"),
                                  command=self.toggle)
        self._tab.pack(fill="x", padx=3, pady=(5, 3))
        self._label = ctk.CTkLabel(self, text=title, text_color=accent,
                                   font=("", 11), anchor="w")
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent",
                                           scrollbar_button_color=accent,
                                           width=COL_W - 24)

    def set_accent(self, accent, tint):
        """Restyle for a different streaming service."""
        self._accent = accent
        self.configure(fg_color=tint, border_color=accent)
        self._tab.configure(hover_color=accent, text_color=accent)
        self._label.configure(text_color=accent)
        self.body.configure(scrollbar_button_color=accent)

    def toggle(self):
        self._open = not self._open
        if self._open:
            self.configure(width=COL_W)
            self._tab.configure(text="\u25bc  " + self._title)
            self.body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        else:
            self.body.pack_forget()
            self._tab.configure(text="\u25b6")
            self.configure(width=COL_MIN)


class ProgressOverlay(ctk.CTkToplevel):
    """Borderless window that floats over the app so it can be truly translucent.

    Tk cannot alpha-blend one widget against its siblings, but it can alpha a
    whole window, so the progress panel lives in its own frameless Toplevel
    pinned to the bottom of the main window.
    """

    ALPHA = 0.82

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.ok = True
        try:
            self.overrideredirect(True)
            self.wm_attributes("-alpha", self.ALPHA)
            self.wm_attributes("-topmost", True)
        except Exception:
            self.ok = False
        try:
            self.configure(fg_color=SURFACE)
        except Exception:
            pass
        self.panel = LogPanel(self)
        self.panel.pack(fill="both", expand=True)
        app.bind("<Configure>", self._follow, add="+")

    def _place(self, rows=4):
        try:
            h = 52 + 20 * max(1, rows)
            w = max(320, self.app.winfo_width() - 24)
            x = self.app.winfo_rootx() + 12
            y = self.app.winfo_rooty() + self.app.winfo_height() - h - 12
            self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:
            pass

    def show_errors(self, errors: list):
        """Keep the panel up after a failed run, listing what actually broke."""
        self.panel.show_stages([])
        self.panel.set_error_list(errors)
        self._place(rows=min(len(errors), 6) + 1)
        self.deiconify()
        self.lift()

    def _follow(self, _e=None):
        if self.winfo_viewable():
            self._place(self._rows)

    _rows = 4

    def show(self, rows):
        self._rows = rows
        self._place(rows)
        self.deiconify()
        self.lift()

    def hide(self):
        self.withdraw()


# ── Log panel ─────────────────────────────────────────────────────────────────

class LogPanel(ctk.CTkFrame):
    def __init__(self, parent):
        # Tk has no real per-widget alpha, so the "translucent" look is a fill
        # blended halfway between the panel colour and the page behind it.
        super().__init__(parent, fg_color=GLASS, corner_radius=10,
                         border_width=1, border_color=BORDER)
        self.STAGES = [("download", "Download",   RED),
                       ("stems",    "Stems",      PURPLE),
                       ("midi",     "MIDI",       GREEN),
                       ("gp",       "Guitar Pro", BLUE)]
        self._rows, self._bars, self._pcts = {}, {}, {}
        self._bar_host = ctk.CTkFrame(self, fg_color="transparent")
        self._bar_host.pack(fill="x", padx=10, pady=(10, 4))
        for key, text, colour in self.STAGES:
            row = ctk.CTkFrame(self._bar_host, fg_color="transparent")
            ctk.CTkLabel(row, text=text, text_color=MUTED, anchor="w",
                         width=74, font=("", 10)).pack(side="left")
            pct = ctk.CTkLabel(row, text="", text_color=MUTED, width=36,
                               anchor="e", font=("", 10))
            pct.pack(side="right")
            bar = ctk.CTkProgressBar(row, height=9, corner_radius=5,
                                     fg_color=SURFACE2, progress_color=colour)
            bar.set(0)
            bar.pack(side="left", fill="x", expand=True, padx=8)
            self._rows[key], self._bars[key], self._pcts[key] = row, bar, pct

        self._line = ctk.CTkLabel(self, text="", text_color=MUTED, anchor="w",
                                  font=("", 10))
        self._line.pack(fill="x", padx=14, pady=(0, 10))

    def show_stages(self, keys):
        """Show only the stages this run will actually touch."""
        for key, _t, _c in self.STAGES:
            self._rows[key].pack_forget()
        for key, _t, _c in self.STAGES:
            if key in keys:
                self._rows[key].pack(fill="x", pady=2)

    def set_error_list(self, errors: list):
        for w in self._bar_host.winfo_children():
            w.pack_forget()
        if not hasattr(self, "_err_host"):
            self._err_host = ctk.CTkFrame(self, fg_color="transparent")
        for w in self._err_host.winfo_children():
            w.destroy()
        self._err_host.pack(fill="x", padx=12, pady=(8, 2), before=self._line)
        ctk.CTkLabel(self._err_host, text="Errors", text_color="#f06060", anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x")
        for e in errors[:6]:
            ctk.CTkLabel(self._err_host, text="• " + str(e), text_color="#f0a0a0",
                         anchor="w", justify="left", font=("", 10),
                         wraplength=560).pack(fill="x", padx=(6, 0))
        if len(errors) > 6:
            ctk.CTkLabel(self._err_host, text=f"…and {len(errors)-6} more",
                         text_color=MUTED, anchor="w", font=("", 10)).pack(fill="x", padx=(6, 0))

    def clear_errors(self):
        if hasattr(self, "_err_host"):
            self._err_host.pack_forget()

    def set_stage(self, key: str, fraction: float):
        if key not in self._bars:
            return
        fraction = max(0.0, min(1.0, fraction))
        self._bars[key].set(fraction)
        self._pcts[key].configure(text="" if fraction == 0 else f"{int(fraction*100)}%")

    def reset_stages(self):
        for k in self._bars:
            self._bars[k].set(0)
            self._pcts[k].configure(text="")
            self._rows[k].pack_forget()

    def append(self, line: str):
        self._line.configure(text=line.strip()[:150])

    def clear(self):
        self._line.configure(text="")


# ── Streaming panel ───────────────────────────────────────────────────────────

class TrackRow(ctk.CTkFrame):
    def __init__(self, parent, track: dict, on_select):
        super().__init__(parent, fg_color="transparent", corner_radius=6, cursor="hand2")
        self.track = track
        self._on_select = on_select
        self._selected = False
        self._build()
        _bind_click(self, lambda _: self._on_select(self))

    def _build(self):
        self.pack(fill="x", pady=1)
        name   = self.track.get("trackName", "Unknown")
        artist = self.track.get("artistName", "")
        album  = self.track.get("collectionName", "")
        meta   = f"{artist}  ·  {album}" if album else artist
        inner  = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(inner, text=name, anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x")
        ctk.CTkLabel(inner, text=meta, anchor="w",
                     text_color=MUTED, font=("", 12)).pack(fill="x")

    def set_selected(self, v: bool):
        self._selected = v
        self.configure(
            fg_color=SURFACE if v else "transparent",
            border_width=1 if v else 0,
            border_color=BLUE if v else BORDER,
        )


class StreamingPanel(ctk.CTkFrame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.app = app
        self._tracks: list[dict] = []
        self._rows: list[TrackRow] = []
        self._selected_idx: int | None = None
        self._var_orig = ctk.BooleanVar(value=True)
        self._var_od   = ctk.BooleanVar(value=True)
        self._var_nd   = ctk.BooleanVar(value=True)
        self._build()

    def _build(self):
        prov = self.app.provider()
        # Prominent panel header, carrying the service switcher
        top = ctk.CTkFrame(self, fg_color=SURFACE2, height=44, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        self._title_lbl = ctk.CTkLabel(top, text=prov.name, text_color=prov.accent,
                                       font=ctk.CTkFont(size=14, weight="bold"))
        self._title_lbl.pack(side="left", padx=16)
        self._prov_menu = ctk.CTkOptionMenu(
            top, width=124, height=26, font=("", 11),
            values=[providers.get(x).name for x in providers.ORDER],
            fg_color=SURFACE, button_color=prov.accent, button_hover_color=prov.dim,
            command=self._on_provider_pick)
        self._prov_menu.set(prov.name)
        self._prov_menu.pack(side="right", padx=12)
        self._rule = ctk.CTkFrame(self, height=2, fg_color=prov.accent, corner_radius=0)
        self._rule.pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        self._body = body

        self._scroll = ctk.CTkScrollableFrame(body, fg_color="transparent",
                                               scrollbar_button_color=BORDER)
        self._scroll.pack(side="right", fill="both", expand=True, padx=8, pady=(0, 0))

        self._empty_lbl = ctk.CTkLabel(self._scroll, text="No results", text_color=MUTED)
        self._empty_lbl.pack(pady=30)

        # Checkboxes — shown only when a track is selected
        self._check_frame = CollapsibleColumn(body, prov.accent, prov.tint, "Options")
        self._check_frame.pack(side="left", fill="y", padx=(8, 0), pady=(0, 4))
        self._stem_vars: dict[str, ctk.BooleanVar] = {}
        self._combo_vars: dict[str, ctk.BooleanVar] = {}
        self._stem_row = ctk.CTkFrame(self._check_frame.body, fg_color="transparent")
        self._stem_row.pack(fill="x", pady=(4, 2))
        self._combo_row = ctk.CTkFrame(self._check_frame.body, fg_color="transparent")
        self._combo_row.pack(fill="x", pady=(6, 4))
        self.refresh_stem_options()

    _EXTRA = [("full", "Full")]

    def _on_provider_pick(self, name: str):
        for pid in providers.ORDER:
            if providers.get(pid).name == name:
                self.app.set_provider(pid)
                return

    def retheme(self):
        """Repaint everything that carries the service's brand colour."""
        prov = self.app.provider()
        self._title_lbl.configure(text=prov.name, text_color=prov.accent)
        self._prov_menu.configure(button_color=prov.accent, button_hover_color=prov.dim)
        self._prov_menu.set(prov.name)
        self._rule.configure(fg_color=prov.accent)
        self._check_frame.set_accent(prov.accent, prov.tint)
        self.refresh_stem_options()   # rebuilds the two captions in the new colour
        self.populate([])             # results belong to the old service

    def refresh_stem_options(self):
        """Rebuild the stem checkboxes for the model chosen in Settings."""
        sources = STEM_SOURCES.get(getattr(self.app, "demucs_model", "htdemucs"),
                                   STEM_SOURCES["htdemucs"])
        opts = self._EXTRA + [(s, s.capitalize()) for s in sources]

        for w in self._stem_row.winfo_children():
            w.destroy()
        ctk.CTkLabel(self._stem_row, text="Download",
                     text_color=self.app.provider().accent,
                     font=("", 10), anchor="w").pack(fill="x", padx=2, pady=(0, 2))
        kept = dict(self._stem_vars); self._stem_vars.clear()
        for key, text in opts:
            saved = getattr(self.app, "stem_sel", None) or {}
            var = kept.get(key) or ctk.BooleanVar(
                value=bool(saved.get(key, key in ("full", "drums"))))
            var.trace_add("write", lambda *_: self._remember())
            self._stem_vars[key] = var
            ctk.CTkCheckBox(self._stem_row, text=text, variable=var,
                            font=("", 11), checkbox_width=15, checkbox_height=15
                            ).pack(fill="x", anchor="w", padx=2, pady=1)

        for w in self._combo_row.winfo_children():
            w.destroy()
        ctk.CTkLabel(self._combo_row, text="Combined",
                     text_color=self.app.provider().accent,
                     font=("", 10), anchor="w").pack(fill="x", padx=2, pady=(0, 2))
        kept = dict(self._combo_vars); self._combo_vars.clear()
        for src in sources:
            saved = getattr(self.app, "combo_sel", None) or {}
            var = kept.get(src) or ctk.BooleanVar(value=bool(saved.get(src, False)))
            var.trace_add("write", lambda *_: self._remember())
            self._combo_vars[src] = var
            ctk.CTkCheckBox(self._combo_row, text=src.capitalize(), variable=var,
                            font=("", 11), checkbox_width=15, checkbox_height=15
                            ).pack(fill="x", anchor="w", padx=2, pady=1)

    def _remember(self):
        """Persist the stem / combined tick boxes so they survive a restart."""
        app = self.app
        sel = dict(getattr(app, "stem_sel", None) or {})
        sel.update({k: bool(v.get()) for k, v in self._stem_vars.items()})
        app.stem_sel = sel
        combo = dict(getattr(app, "combo_sel", None) or {})
        combo.update({k: bool(v.get()) for k, v in self._combo_vars.items()})
        app.combo_sel = combo
        if hasattr(app, "persist"):
            app.persist()

    def selected_stems(self) -> list[str]:
        return [k for k, v in self._stem_vars.items()
                if v.get() and k not in ("full", "drums")]

    def combined_stems(self) -> list[str]:
        return [k for k, v in self._combo_vars.items() if v.get()]

    def populate(self, tracks: list[dict]):
        self._tracks = tracks
        self._rows = []
        self._selected_idx = None
        for w in self._scroll.winfo_children():
            w.destroy()
        if not tracks:
            self._empty_lbl = ctk.CTkLabel(self._scroll, text="No results", text_color=MUTED)
            self._empty_lbl.pack(pady=30)
            return
        for t in tracks:
            row = TrackRow(self._scroll, t, self._on_row_click)
            self._rows.append(row)

    def _on_row_click(self, row: TrackRow):
        idx = self._rows.index(row)
        if self._selected_idx == idx:
            row.set_selected(False)
            self._selected_idx = None
        else:
            if self._selected_idx is not None:
                self._rows[self._selected_idx].set_selected(False)
            self._selected_idx = idx
            row.set_selected(True)

    @property
    def selected_track(self) -> dict | None:
        return self._tracks[self._selected_idx] if self._selected_idx is not None else None

    @property
    def download_original(self) -> bool:
        return self._stem_vars['full'].get()

    @property
    def download_od(self) -> bool:
        return self._stem_vars['drums'].get()

    @property
    def download_nd(self) -> bool:
        # "No drums" is gone: tick everything except Drums under "Combined".
        return False


# ── Songsterr panel ───────────────────────────────────────────────────────────

class SSResultRow(ctk.CTkFrame):
    def __init__(self, parent, song: dict, on_select):
        super().__init__(parent, fg_color="transparent", corner_radius=6, cursor="hand2")
        self.song = song
        self._on_select = on_select
        self._selected = False
        self._build()
        _bind_click(self, lambda _: self._on_select(self))

    def _build(self):
        self.pack(fill="x", pady=1)
        name   = self.song.get("title", "Unknown")
        artist = self.song.get("artist", "")
        seen_icons: list[str] = []
        seen_set: set[str] = set()
        for t in self.song.get("tracks", []):
            _, icon = _instrument_category(t.get("instrumentId", -1))
            if icon not in seen_set:
                seen_icons.append(icon)
                seen_set.add(icon)
        icons = "  " + " ".join(seen_icons) if seen_icons else ""
        label = f"{artist} – {name}{icons}"
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(inner, text=label, anchor="w", font=("", 12)).pack(fill="x")

    def set_selected(self, v: bool):
        self._selected = v
        self.configure(
            fg_color=SURFACE if v else "transparent",
            border_width=1 if v else 0,
            border_color=BLUE if v else BORDER,
        )


class SongsterrPanel(ctk.CTkFrame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.app = app
        self._songs: list[dict] = []
        self._rows: list[SSResultRow] = []
        self._selected_idx: int | None = None
        self._selected_rev: dict | None = None
        self._instrument_vars: dict[str, ctk.BooleanVar] = {}
        self._build()

    def _header_text(self) -> str:
        """Name the formats this panel will actually produce."""
        gp   = bool(getattr(self.app, "want_gp", True))
        midi = bool(getattr(self.app, "want_midi", False))
        if gp and midi:
            return "Guitar Pro & MIDI"
        if gp:
            return "Guitar Pro"
        if midi:
            return "MIDI"
        return "Songsterr"

    def refresh_header(self):
        self._title_lbl.configure(text=self._header_text())

    def _build(self):
        # Prominent panel header with format selector inline
        top = ctk.CTkFrame(self, fg_color=SURFACE2, height=44, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)
        self._title_lbl = ctk.CTkLabel(top, text=self._header_text(), text_color=BLUE,
                                       font=ctk.CTkFont(size=14, weight="bold"))
        self._title_lbl.pack(side="left", padx=16)
        # Output formats live in Settings; the header names whichever are on.
        ctk.CTkFrame(self, height=2, fg_color=BLUE, corner_radius=0).pack(fill="x")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        # Track selector — a vertical column to the left of the song titles.
        # Packed only once a song is selected.
        self._check_frame = CollapsibleColumn(body, BLUE, SS_TINT, "Tracks")
        self._check_frame.pack(side="left", fill="y", padx=(8, 0), pady=(0, 4))
        self._check_rows_frame = ctk.CTkFrame(self._check_frame.body, fg_color="transparent")
        self._check_rows_frame.pack(fill="x")
        self._audio_frame = ctk.CTkFrame(self._check_frame.body, fg_color="transparent")
        self._audio_frame.pack(fill="x", pady=(12, 2))
        self._audio_vars: dict[str, ctk.BooleanVar] = {}
        self.refresh_audio_options()

        self._scroll = ctk.CTkScrollableFrame(body, fg_color="transparent",
                                               scrollbar_button_color=BORDER)
        self._scroll.pack(side="right", fill="both", expand=True, padx=8, pady=(0, 0))
        self._empty_lbl = ctk.CTkLabel(self._scroll, text="No results", text_color=MUTED)
        self._empty_lbl.pack(pady=30)

        self._rev_label = ctk.CTkLabel(self, text="", text_color=MUTED,
                                        font=("", 11), wraplength=340)
        self._rev_label.pack(fill="x", padx=14, pady=(6, 2))

    def populate(self, songs: list[dict]):
        self._songs = songs
        self._rows = []
        self._selected_idx = None
        self._selected_rev = None
        self._instrument_vars.clear()
        self._rev_label.configure(text="")
        for w in self._scroll.winfo_children():
            w.destroy()
        if not songs:
            self._empty_lbl = ctk.CTkLabel(self._scroll, text="No results", text_color=MUTED)
            self._empty_lbl.pack(pady=30)
            return
        for s in songs:
            row = SSResultRow(self._scroll, s, self._on_row_click)
            self._rows.append(row)

    def refresh_audio_options(self):
        """Embed-audio controls, rebuilt when the separation model changes."""
        for w in self._audio_frame.winfo_children():
            w.destroy()
        ctk.CTkFrame(self._audio_frame, height=1, fg_color=BLUE).pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(self._audio_frame, text="Embed Audio", text_color=BLUE,
                     font=("", 10), anchor="w").pack(fill="x", padx=2, pady=(0, 2))

        self._var_gpaudio = ctk.BooleanVar(value=bool(getattr(self.app, "gp_with_audio", False)))
        self._var_gpaudio.trace_add("write", lambda *_: self._remember_audio())
        ctk.CTkCheckBox(self._audio_frame, text="Include audio", variable=self._var_gpaudio,
                        font=("", 11), checkbox_width=15, checkbox_height=15
                        ).pack(fill="x", anchor="w", padx=2, pady=1)

        saved = getattr(self.app, "gp_audio_sel", None) or {}
        kept = dict(self._audio_vars)
        self._audio_vars.clear()
        for src in STEM_SOURCES.get(getattr(self.app, "demucs_model", "htdemucs"),
                                    STEM_SOURCES["htdemucs"]):
            var = kept.get(src) or ctk.BooleanVar(value=bool(saved.get(src, src != "drums")))
            var.trace_add("write", lambda *_: self._remember_audio())
            self._audio_vars[src] = var
            ctk.CTkCheckBox(self._audio_frame, text=src.capitalize(), variable=var,
                            font=("", 11), checkbox_width=15, checkbox_height=15
                            ).pack(fill="x", anchor="w", padx=(12, 2), pady=1)

    def _remember_audio(self):
        self.app.gp_with_audio = bool(self._var_gpaudio.get())
        sel = dict(getattr(self.app, "gp_audio_sel", None) or {})
        sel.update({k: bool(v.get()) for k, v in self._audio_vars.items()})
        self.app.gp_audio_sel = sel
        if hasattr(self.app, "persist"):
            self.app.persist()

    def _populate_checkboxes(self, song: dict):
        for w in self._check_rows_frame.winfo_children():
            w.destroy()
        self._instrument_vars.clear()

        tracks = song.get("tracks", [])
        seen_cats: list[tuple[str, str]] = []
        seen_set: set[str] = set()
        for t in tracks:
            cat, icon = _instrument_category(t.get("instrumentId", -1))
            if cat not in seen_set:
                seen_cats.append((cat, icon))
                seen_set.add(cat)

        if not seen_cats:
            return

        for cat, icon in seen_cats:
            var = ctk.BooleanVar(value=True)
            self._instrument_vars[cat] = var
            ctk.CTkCheckBox(self._check_rows_frame, text=f"{icon} {cat}", variable=var,
                            font=("", 11), checkbox_width=15, checkbox_height=15
                            ).pack(fill="x", anchor="w", padx=2, pady=1)


    def _on_row_click(self, row: SSResultRow):
        idx = self._rows.index(row)
        if self._selected_idx == idx:
            row.set_selected(False)
            self._selected_idx = None
            self._selected_rev = None
            self._rev_label.configure(text="")
            self._instrument_vars.clear()
            for w in self._check_rows_frame.winfo_children():
                w.destroy()
            return
        if self._selected_idx is not None:
            self._rows[self._selected_idx].set_selected(False)
        self._selected_idx = idx
        self._selected_rev = None
        row.set_selected(True)
        self._populate_checkboxes(row.song)
        self._rev_label.configure(text="Loading revision…", text_color=MUTED)
        threading.Thread(target=self._load_rev, args=(row.song["songId"],),
                         daemon=True).start()

    def _load_rev(self, song_id: int):
        try:
            revs = sapi.get_revisions(song_id)
            if revs:
                rev = max(revs, key=lambda r: r["createdAt"])
                self.after(0, self._rev_loaded, rev, None)
            else:
                self.after(0, self._rev_loaded, None, "No revisions found")
        except Exception as e:
            self.after(0, self._rev_loaded, None, str(e))

    def _rev_loaded(self, rev: dict | None, error: str | None):
        if error:
            self._rev_label.configure(text=f"Error: {error}", text_color="#f06060")
        else:
            self._selected_rev = rev
            date   = rev["createdAt"][:10]
            author = rev.get("author", {}).get("name", "?")
            self._rev_label.configure(
                text=f"Latest revision: {date} by {author} — ready to download",
                text_color=MUTED)

    @property
    def selected_song(self) -> dict | None:
        return self._songs[self._selected_idx] if self._selected_idx is not None else None

    @property
    def selected_rev(self) -> dict | None:
        return self._selected_rev

    @property
    def want_midi(self) -> bool:
        return bool(getattr(self.app, "want_midi", True))

    @property
    def want_gp(self) -> bool:
        return bool(getattr(self.app, "want_gp", True))

    @property
    def selected_categories(self) -> set[str] | None:
        """Checked instrument categories, or None if no checkboxes are shown (means all)."""
        if not self._instrument_vars:
            return None
        return {cat for cat, var in self._instrument_vars.items() if var.get()}


# ── Main App ──────────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"Practice Helper {__version__}")
        self.geometry("1100x740")
        self.minsize(820, 580)
        self.configure(fg_color=BG)

        cfg = load_cfg()
        self._first_run    = not cfg
        self.provider_id   = cfg.get("provider", providers.DEFAULT_PROVIDER)
        self.creds         = dict(cfg.get("creds") or {})
        self.download_cmds = dict(cfg.get("download_cmds") or {})
        self.tools_path    = cfg.get("tools_path", "")
        # Pre-provider configs kept the Apple cookies at the top level.
        if cfg.get("cookies_path") and not self.creds.get("apple_cookies"):
            self.creds["apple_cookies"] = cfg["cookies_path"]
        self.output_path   = cfg.get("output_path",   DEFAULT_OUTPUT)
        self.stems_path    = cfg.get("stems_path",    DEFAULT_STEMS)
        self.notation_mode = cfg.get("notation_mode", "standard")
        self.demucs_model  = cfg.get("demucs_model",  "htdemucs")
        self.want_midi     = cfg.get("want_midi", True)
        self.want_gp       = cfg.get("want_gp",   True)
        self.stem_sel      = cfg.get("stem_sel",  {"full": True, "drums": True})
        self.combo_sel     = cfg.get("combo_sel", {})
        self.gp_with_audio = cfg.get("gp_with_audio", False)
        self.gp_audio_sel  = cfg.get("gp_audio_sel", {})

        self._build()

    def persist(self):
        save_cfg({
            "provider":      self.provider_id,
            "creds":         self.creds,
            "download_cmds": self.download_cmds,
            "tools_path":    self.tools_path,
            "output_path":   self.output_path,
            "stems_path":    self.stems_path,
            "notation_mode": self.notation_mode,
            "demucs_model":  self.demucs_model,
            "want_midi":     self.want_midi,
            "want_gp":       self.want_gp,
            "stem_sel":      self.stem_sel,
            "combo_sel":     self.combo_sel,
            "gp_with_audio": self.gp_with_audio,
            "gp_audio_sel":  self.gp_audio_sel,
        })

    def _build(self):
        # Header — title centered, settings at right
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, height=62, corner_radius=0)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        hdr.columnconfigure(1, weight=1)
        hdr.rowconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="Practice Helper",
                     font=ctk.CTkFont(family="Avenir Next", size=21, weight="bold"),
                     ).grid(row=0, column=1, sticky="nsew")
        ctk.CTkButton(hdr, text="Settings", width=80, height=30,
                      fg_color=SURFACE2, hover_color=BORDER,
                      border_width=1, border_color=BORDER,
                      command=lambda: SettingsDialog(self)).grid(row=0, column=2, sticky="e", padx=16)
        ctk.CTkFrame(self, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x")

        # Search bar
        bar = ctk.CTkFrame(self, fg_color=SURFACE, height=62, corner_radius=0)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=11)
        self._search_entry = ctk.CTkEntry(inner, placeholder_text="Search for a song…",
                                           height=38)
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._search_entry.bind("<Return>", lambda _: self._do_search())
        self._search_btn = ctk.CTkButton(inner, text="Search", width=90, height=38,
                                          fg_color=RED, hover_color=RED_DIM,
                                          command=self._do_search)
        self._search_btn.pack(side="left")
        ctk.CTkFrame(self, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x")

        # Two-panel results
        panes = ctk.CTkFrame(self, fg_color="transparent")
        panes.pack(fill="both", expand=True)
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=0)
        panes.columnconfigure(2, weight=1)
        panes.rowconfigure(0, weight=1)

        self.am_panel = StreamingPanel(panes, self)
        self.am_panel.grid(row=0, column=0, sticky="nsew")
        ctk.CTkFrame(panes, width=1, fg_color=BORDER,
                     corner_radius=0).grid(row=0, column=1, sticky="ns")
        self.ss_panel = SongsterrPanel(panes, self)
        self.ss_panel.grid(row=0, column=2, sticky="nsew")

        ctk.CTkFrame(self, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x")

        # Bottom action bar — Process button centered
        bottom = ctk.CTkFrame(self, fg_color=SURFACE, height=56, corner_radius=0)
        bottom.pack(fill="x")
        bottom.pack_propagate(False)
        bottom.columnconfigure(0, weight=1)
        bottom.columnconfigure(1, weight=0)
        bottom.columnconfigure(2, weight=1)
        bottom.rowconfigure(0, weight=1)
        self._process_btn = ctk.CTkButton(bottom, text="Process", width=120, height=36,
                                           fg_color=RED, hover_color=RED_DIM,
                                           command=self._do_process)
        self._process_btn.grid(row=0, column=1)
        self._status = ctk.CTkLabel(bottom, text="Search for a song to get started.",
                                    text_color=MUTED, anchor="w", font=("", 12))
        self._status.grid(row=0, column=0, sticky="w", padx=16)

        # Log
        try:
            self._overlay = ProgressOverlay(self)
            self._log = self._overlay.panel
        except Exception:
            self._overlay = None
            self._log = LogPanel(self)

        self._retheme()
        if self._first_run:
            self.after(400, self._welcome)

    def _welcome(self):
        """First launch: nothing is configured yet, so say so and offer Settings."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Welcome")
        dlg.geometry("470x300")
        dlg.resizable(False, False)
        dlg.grab_set()
        ctk.CTkLabel(dlg, text="Welcome to Practice Helper",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(26, 8))
        ctk.CTkLabel(
            dlg, wraplength=400, justify="left", text_color=MUTED, font=("", 12),
            text=("Nothing is set up yet. In Settings you can choose a streaming "
                  "service, enter its credentials, and pick where songs are saved.\n\n"
                  "Songsterr tabs and Guitar Pro export work straight away — no "
                  "account needed. A streaming service is only required for audio "
                  "and stem separation.")
        ).pack(padx=30, pady=(0, 14), fill="x")
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(pady=6)
        ctk.CTkButton(row, text="Open Settings", width=140, height=36,
                      fg_color=self.provider().accent,
                      hover_color=self.provider().dim,
                      command=lambda: (dlg.destroy(), SettingsDialog(self))
                      ).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Later", width=100, height=36,
                      fg_color=SURFACE2, hover_color=BORDER,
                      command=dlg.destroy).pack(side="left", padx=6)

    def provider(self):
        return providers.get(self.provider_id)

    def set_provider(self, pid: str):
        if pid == self.provider_id:
            return
        self.provider_id = pid
        self.persist()
        self.am_panel.retheme()
        self._retheme()

    def _retheme(self):
        """Re-colour the shared chrome for the active service."""
        prov = self.provider()
        for btn in (self._search_btn, self._process_btn):
            btn.configure(fg_color=prov.accent, hover_color=prov.dim)

    def _song_dir(self, label: str) -> Path:
        return Path(self.stems_path) / label

    # ── Search ────────────────────────────────────────────────────────────────

    def _do_search(self):
        q = self._search_entry.get().strip()
        if not q:
            return
        self._search_btn.configure(state="disabled", text="…")
        self._status.configure(text="Searching…", text_color=MUTED)
        self._log.clear()
        self._log.reset_stages()
        if self._overlay is not None:
            self._overlay.hide()
        threading.Thread(target=self._search_thread, args=(q,), daemon=True).start()

    def _search_thread(self, q: str):
        am_results, ss_results = [], []
        am_err = ss_err = None

        prov = self.provider()

        def fetch_am():
            nonlocal am_results, am_err
            try:
                missing = prov.missing(self.creds)
                if missing:
                    raise RuntimeError(
                        "not set up yet — add " +
                        ", ".join(f.label.lower() for f in missing) + " in Settings")
                am_results = prov.search(q, self.creds, limit=20)
            except Exception as e:
                am_err = str(e)

        def fetch_ss():
            nonlocal ss_results, ss_err
            try:
                ss_results = sapi.search(q, size=20)
            except Exception as e:
                ss_err = str(e)

        t1 = threading.Thread(target=fetch_am, daemon=True)
        t2 = threading.Thread(target=fetch_ss, daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.after(0, self._search_done, am_results, ss_results, am_err, ss_err)

    def _search_done(self, am, ss, am_err, ss_err):
        self._search_btn.configure(state="normal", text="Search")
        self.am_panel.populate(am)
        self.ss_panel.populate(ss)
        if am_err or ss_err:
            parts = []
            if am_err: parts.append(f"{self.provider().name}: {am_err}")
            if ss_err: parts.append(f"Songsterr: {ss_err}")
            self._status.configure(text="  |  ".join(parts), text_color="#f06060")
        else:
            self._status.configure(
                text=f"{len(am)} {self.provider().name}  ·  {len(ss)} Songsterr results",
                text_color=MUTED)

    # ── Process ───────────────────────────────────────────────────────────────

    def _do_process(self):
        am_track  = self.am_panel.selected_track
        ss_song   = self.ss_panel.selected_song
        ss_rev    = self.ss_panel.selected_rev
        ss_cats   = self.ss_panel.selected_categories  # read on main thread before spawning
        ss_midi   = self.ss_panel.want_midi
        ss_gp     = self.ss_panel.want_gp
        if not am_track and not ss_song:
            self._status.configure(text="Select at least one result first.",
                                   text_color="#f06060")
            return
        if ss_song and not ss_midi and not ss_gp:
            self._status.configure(text="Select at least one Songsterr format (MIDI or Guitar Pro).",
                                   text_color="#f06060")
            return
        if getattr(self, "gp_with_audio", False) and not am_track and ss_song:
            label = sanitize("%s - %s" % (ss_song.get("artist", "Unknown"),
                                          ss_song.get("title", "Unknown")))
            if not list(self._song_dir(label).glob("*.wav")):
                self._status.configure(
                    text=("Embed Audio is on, but no %s track is selected — "
                          "the audio comes from its stems."
                          % self.provider().name), text_color="#f0a060")
                return

        self._process_btn.configure(state="disabled", text="Processing…")
        stages = []
        if am_track:
            stages.append("download")
            if (self.am_panel.download_od or self.am_panel.selected_stems()
                    or self.am_panel.combined_stems()):
                stages.append("stems")
        if ss_song and ss_midi:
            stages.append("midi")
        if ss_song and ss_gp:
            stages.append("gp")
        self._show_progress(stages)
        self._status.configure(text="Processing…", text_color=BLUE)
        threading.Thread(target=self._process_thread,
                         args=(am_track, ss_song, ss_rev, ss_cats, ss_midi, ss_gp),
                         daemon=True).start()

    def _log_line(self, line: str):
        self.after(0, self._log.append, line)

    def _progress(self, stage: str, fraction: float):
        self.after(0, self._log.set_stage, stage, fraction)

    def _process_thread(self, am_track, ss_song, ss_rev, ss_cats, ss_midi, ss_gp):
        errors = []

        # Determine folder label from AM track (used by Songsterr download too)
        am_label = None
        if am_track:
            am_name   = am_track.get("trackName", "Unknown")
            am_artist = am_track.get("artistName", "Unknown")
            am_label  = sanitize(f"{am_artist} - {am_name}")

        # Stems the embedded audio needs — these must be extracted too, or the
        # demucs run never produces them.
        audio_stems = []
        if getattr(self, "gp_with_audio", False):
            audio_stems = [k for k, v in (self.gp_audio_sel or {}).items() if v]
        audio_label = am_label
        if not audio_label and ss_song:
            audio_label = sanitize("%s - %s" % (ss_song.get("artist", "Unknown"),
                                                ss_song.get("title", "Unknown")))

        # Songsterr MIDI — runs in parallel with the audio download
        ss_thread = None
        if ss_song and ss_rev:
            ss_thread = threading.Thread(
                target=self._download_midi,
                args=(ss_song, ss_rev, ss_cats, ss_midi, ss_gp, am_label, errors),
                daemon=True)
            ss_thread.start()

        # Streaming-service pipeline
        if am_track:
            url    = am_track.get("trackViewUrl", "")
            label  = am_label
            song_dir = self._song_dir(label)

            need_orig  = self.am_panel.download_original
            need_od    = self.am_panel.download_od
            need_nd    = self.am_panel.download_nd
            sel_stems  = self.am_panel.selected_stems()
            sel_combo  = self.am_panel.combined_stems()
            if audio_stems:
                sel_stems = sorted(set(sel_stems) | {x for x in audio_stems if x != "drums"})
                if "drums" in audio_stems:
                    need_od = True
            need_stems = need_od or need_nd or bool(sel_stems) or bool(sel_combo)

            # Duplicate checks — files now live in per-song subfolder
            od_path   = song_dir / f"{label} (OD).wav"
            nd_path   = song_dir / f"{label} (ND).wav"
            od_exists = od_path.exists()
            nd_exists = nd_path.exists()
            if need_od and od_exists:
                self._log_line(f"  ⤼ Drums already exists, skipping: {od_path.name}")
            if need_nd and nd_exists:
                self._log_line(f"  ⤼ No drums already exists, skipping: {nd_path.name}")
            need_od = need_od and not od_exists
            need_nd = need_nd and not nd_exists
            need_stems = need_od or need_nd or bool(sel_stems) or bool(sel_combo)

            # "Full" is only satisfied once the audio is in the song folder,
            # not merely present somewhere in the download library.
            orig_dest = song_dir / f"{label} (Full).m4a"
            if need_orig and orig_dest.exists():
                self._log_line(f"  ⤼ Full already exists, skipping: {orig_dest.name}")
                need_orig = False

            downloaded = None
            if need_orig or need_stems:
                existing = find_existing_m4a(self.output_path, am_name, am_artist)
                if existing and need_orig:
                    self._log_line(f"  ⤼ Found in library: {existing.name}")
                    downloaded = existing
                elif existing and need_stems:
                    downloaded = existing
                else:
                    before = scan_m4a(self.output_path)
                    self._log_line(f"▶ Downloading: {label}")
                    self._progress("download", 0.05)
                    ok = self._run_downloader(url)
                    self._progress("download", 1.0 if ok else 0.0)
                    if ok:
                        downloaded = (find_new_m4a(before, self.output_path)
                                      or find_existing_m4a(self.output_path, am_name, am_artist))
                        if downloaded:
                            self._log_line(f"  ✓ Saved: {downloaded.name}")
                        else:
                            self._log_line("  ✓ Download complete (file path not detected)")
                    else:
                        errors.append(f"Download failed for {label}")

            if need_orig and downloaded:
                song_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(str(downloaded), str(orig_dest))
                    self._log_line(f"  ✓ Full: {orig_dest.name}")
                except Exception as ce:
                    errors.append(f"could not copy original: {ce}")

            if need_stems and downloaded:
                self._progress("stems", 0.02)
                self._run_demucs(downloaded, label, song_dir, need_od, need_nd, errors,
                                 stems=sel_stems, combo=sel_combo)
            elif need_stems and not downloaded:
                self._log_line("  ✗ Could not locate audio file for stem extraction")
                errors.append("Stem extraction skipped — file not found")

        if ss_thread:
            ss_thread.join()

        if getattr(self, "gp_with_audio", False) and audio_label:
            self._embed_audio(audio_label, errors)

        msg = "Done!" if not errors else f"Done with {len(errors)} error(s)."
        self.after(0, self._process_done, msg, list(errors))

    def _run_downloader(self, url: str) -> bool:
        """Run the active provider's download command.

        The command is a user-supplied template rather than anything baked in:
        no single tool covers every service, so the app substitutes {url},
        {out}, {tmp} and the provider's own credential keys and gets out of the
        way. A provider with no command configured simply reports that.
        """
        prov = self.provider()
        template = (self.download_cmds.get(prov.id) or prov.download_cmd or "").strip()
        if not template:
            self._log_line(f"  ✗ No download command configured for {prov.name}.")
            return False

        tmp = Path(tempfile.mkdtemp(prefix="practice_helper_dl_"))
        subs = dict(self.creds)
        subs.update({"url": url, "out": self.output_path, "tmp": str(tmp)})
        try:
            cmd = [part.format(**subs) for part in shlex.split(template)]
        except KeyError as ke:
            self._log_line(f"  ✗ Download command refers to unknown field {ke}.")
            shutil.rmtree(str(tmp), ignore_errors=True)
            return False
        # Resolve the executable the same way as every other helper tool, so a
        # bare "gamdl" works whether it is on PATH or inside a venv.
        resolved = find_tool(Path(cmd[0]).stem, self.tools_path)
        if resolved:
            cmd[0] = resolved
        elif not Path(cmd[0]).is_file():
            self._log_line(f"  ✗ '{cmd[0]}' not found — is it installed and on your PATH?")
            shutil.rmtree(str(tmp), ignore_errors=True)
            return False
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                m = re.search(r"(\d+)%", line)
                if m:
                    self._progress("stems", int(m.group(1)) / 100.0)
                else:
                    self._log_line(f"  {line}")
            proc.wait()
            self._progress("stems", 1.0)
            return proc.returncode == 0
        except Exception as e:
            self._log_line(f"  Error: {e}")
            return False
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)

    def _accelerator(self) -> str:
        """Pick a demucs device by what torch can actually see.

        Choosing on operating system alone is wrong: plenty of Windows and
        Linux machines have no CUDA GPU, and asking demucs for one there fails
        outright instead of quietly falling back.
        """
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def _to_wav(self, src: Path, dst: Path) -> bool:
        """Convert *src* to 16-bit WAV at *dst*. Returns True on success."""
        if _MAC:
            cmd = ["afconvert", "-f", "WAVE", "-d", "LEI16", str(src), str(dst)]
        else:
            ff = find_tool("ffmpeg", getattr(self, "tools_path", ""))
            if not ff:
                self._log_line("  ✗ ffmpeg not found — install it and put it on your PATH.")
                return False
            cmd = [ff, "-y", "-i", str(src),
                   "-acodec", "pcm_s16le", "-ar", "44100", str(dst)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            self._log_line(f"  ✗ Audio conversion failed: {r.stderr.decode().strip()[:200]}")
        return r.returncode == 0

    def _run_demucs(self, src: Path, label: str, song_dir: Path,
                    want_od: bool, want_nd: bool, errors: list,
                    stems: list | None = None, combo: list | None = None):
        stems = list(stems or [])
        combo = list(combo or [])
        tmp = Path(tempfile.mkdtemp(prefix="drum_hub_demucs_"))
        try:
            # torchaudio can't load m4a without torchcodec — convert to wav first
            if src.suffix.lower() != ".wav":
                wav = tmp / (src.stem + ".wav")
                self._log_line("  Converting to WAV…")
                if not self._to_wav(src, wav):
                    errors.append(f"Audio conversion failed for {src.name}")
                    return
                demucs_input = wav
            else:
                demucs_input = src

            self._log_line(f"▶ Extracting stems from: {src.name}")
            runner = Path(__file__).parent / "demucs_runner.py"
            python = find_tool("python3", self.tools_path) or sys.executable
            accel = self._accelerator()
            model = getattr(self, "demucs_model", "htdemucs")
            sources = STEM_SOURCES.get(model, STEM_SOURCES["htdemucs"])
            stems = [x for x in stems if x in sources]
            combo = [x for x in combo if x in sources]
            # Fast path: nothing but OD/ND wanted -> two-stem split is much quicker.
            simple = not stems and not combo
            cmd = [python, str(runner), "-n", model,
                   "-d", accel, "-o", str(tmp), str(demucs_input)]
            if simple:
                cmd[4:4] = ["--two-stems", "drums"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._log_line(f"  {line}")
            proc.wait()
            if proc.returncode != 0:
                errors.append(f"demucs failed for {src.name}")
                return

            sep_dir = tmp / model / demucs_input.stem
            song_dir.mkdir(parents=True, exist_ok=True)

            if want_od:
                od_src = sep_dir / "drums.wav"
                if od_src.exists():
                    dest = song_dir / f"{label} (OD).wav"
                    shutil.move(str(od_src), str(dest))
                    self._log_line(f"  ✓ Drums: {dest.name}")
                else:
                    errors.append("drums.wav missing after demucs")

            def _write_mix(names: list, dest: Path) -> bool:
                try:
                    import numpy as np, soundfile as sf
                except Exception as ie:
                    errors.append(f"mixing needs numpy+soundfile: {ie}")
                    return False
                mix, rate = None, None
                for nm in names:
                    f = sep_dir / f"{nm}.wav"
                    if not f.exists():
                        continue
                    data, rate = sf.read(str(f), always_2d=True)
                    mix = data if mix is None else mix + data
                if mix is None:
                    return False
                sf.write(str(dest), np.clip(mix, -1.0, 1.0), rate)
                return True

            for name in stems:
                if name == "drums" and want_od:
                    continue          # already written as (OD).wav
                srcf = sep_dir / f"{name}.wav"
                if srcf.exists():
                    dest = song_dir / f"{label} ({name}).wav"
                    shutil.move(str(srcf), str(dest))
                    self._log_line(f"  ✓ {name}: {dest.name}")
                else:
                    errors.append(f"{name}.wav missing after demucs")

            if combo:
                dest = song_dir / f"{label} ({'+'.join(combo)}).wav"
                if _write_mix(combo, dest):
                    self._log_line(f"  ✓ combined: {dest.name}")
                else:
                    errors.append("combined mix failed")

            if want_nd:
                dest = song_dir / f"{label} (ND).wav"
                nd_src = sep_dir / "no_drums.wav"
                if nd_src.exists():
                    # two-stem fast path produced it directly
                    shutil.move(str(nd_src), str(dest))
                    self._log_line(f"  ✓ No drums: {dest.name}")
                elif _write_mix([x for x in sources if x != "drums"], dest):
                    # full separation: sum every non-drum source back together
                    self._log_line(f"  ✓ ND: {dest.name}")
                else:
                    errors.append("could not build ND (no-drums) mix")
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)

    def _download_midi(self, song: dict, rev: dict, categories: set[str] | None,
                       want_midi: bool, want_gp: bool, folder_name: str | None,
                       errors: list):
        artist   = song.get("artist", "Unknown")
        title    = song.get("title", "Unknown")
        label    = folder_name or sanitize(f"{artist} - {title}")
        song_dir = self._song_dir(label)
        mid_dest = song_dir / f"{label}.mid"
        gp_dest  = song_dir / f"{label}.gp"

        need_midi = want_midi and not mid_dest.exists()
        stale_gp  = (gp_dest.exists() and
                     songsterr_to_gp.file_version(gp_dest) != songsterr_to_gp.WRITER_VERSION)
        need_gp   = want_gp and (not gp_dest.exists() or stale_gp)
        if want_midi and not need_midi:
            self._log_line(f"  ⤼ MIDI already exists, skipping: {mid_dest.name}")
        if want_gp and not need_gp:
            self._log_line(f"  ⤼ Guitar Pro already exists, skipping: {gp_dest.name}")
        elif want_gp and stale_gp:
            self._log_line(f"  ↻ Rebuilding {gp_dest.name} (written by an older converter)")
        if not need_midi and not need_gp:
            return

        self._log_line(f"▶ Downloading: {label}")
        try:
            song_id  = song["songId"]
            rev_id   = rev["revisionId"]
            full_rev = sapi.get_revision(rev_id)
            image    = full_rev.get("image") or ""
            tracks   = full_rev.get("tracks", [])

            # Download track data. Guitar Pro gets the full arrangement (all
            # tracks); MIDI respects the category filter.
            all_track_meta_data: list[tuple[dict, dict]] = []
            for i, t in enumerate(tracks):
                if t.get("isEmpty"):
                    continue
                data = sapi.get_track_data(song_id, rev_id, image, i)
                all_track_meta_data.append((t, data))

            if not all_track_meta_data:
                raise ValueError("No tracks found in this revision.")

            song_dir.mkdir(parents=True, exist_ok=True)

            # ── MIDI (category-filtered) ───────────────────────────────────────
            if need_midi:
                midi_data = [
                    d for t, d in all_track_meta_data
                    if categories is None or
                       _instrument_category(t.get("instrumentId", -1))[0] in categories
                ]
                if not midi_data:
                    self._log_line("  ✗ No tracks matched the selected categories for MIDI")
                    errors.append("No matching tracks for MIDI")
                else:
                    self._progress("midi", 0.3)
                    mid = conv.convert(full_rev, midi_data)
                    mid.save(str(mid_dest))
                    self._progress("midi", 1.0)
                    self._log_line(f"  ✓ MIDI: {mid_dest.name} ({len(midi_data)} track(s))")

            # ── Guitar Pro (full arrangement) ─────────────────────────────────
            if need_gp:
                gp_data = [d for _, d in all_track_meta_data]
                self._log_line(
                    f"  Converting {len(gp_data)} track(s) to Guitar Pro…")
                try:
                    # Written straight from the Songsterr data. Going via MIDI
                    # (the old midi_to_gp path) lost tuning, string/fret and
                    # snapped every track onto a single 16th or triplet grid,
                    # which destroyed 32nds and triplets.
                    self._progress("gp", 0.3)
                    warns = songsterr_to_gp.convert(
                        full_rev, gp_data, gp_dest,
                        notation=getattr(self, 'notation_mode', 'standard'))
                    for w in warns:
                        self._log_line(f"  ⚠ {w}")
                    self._progress("gp", 1.0)
                    self._log_line(
                        f"  ✓ Guitar Pro: {gp_dest.name} ({len(gp_data)} track(s))")
                except Exception as ge:
                    errors.append(f"Guitar Pro conversion failed: {ge}")
                    self._log_line(f"  ✗ Guitar Pro failed: {ge}")

        except Exception as e:
            errors.append(f"Download failed: {e}")
            self._log_line(f"  ✗ Failed: {e}")

    def _embed_audio(self, label: str, errors: list):
        """Mix the chosen stems and attach them to the .gp, aligned to the drums."""
        song_dir = self._song_dir(label)
        gp = song_dir / f"{label}.gp"
        if not gp.exists():
            msg = f"audio: no {gp.name} was produced"
            errors.append(msg); self._log_line("  ⚠ " + msg)
            return
        wanted = [k for k, v in (self.gp_audio_sel or {}).items() if v]
        if not wanted:
            msg = "audio: no stems ticked under Embed Audio"
            errors.append(msg); self._log_line("  ⚠ " + msg)
            return
        sources = set(STEM_SOURCES.get(getattr(self, "demucs_model", "htdemucs"),
                                       STEM_SOURCES["htdemucs"]))
        nd = song_dir / f"{label} (ND).wav"
        if set(wanted) == sources - {"drums"} and nd.exists():
            self._log_line("  Using existing no-drums mix for the audio")
            return self._attach(gp, song_dir, label, [str(nd)], errors)

        parts, missing = [], []
        for name in wanted:
            cand = [song_dir / f"{label} ({name}).wav"]
            if name == "drums":
                cand.append(song_dir / f"{label} (OD).wav")
            hit = next((p for p in cand if p.exists()), None)
            (parts.append(str(hit)) if hit else missing.append(name))
        if not parts:
            msg = ("audio: no stems on disk (%s) — select the streaming track "
                   "as well so they can be extracted" % ", ".join(wanted))
            errors.append(msg); self._log_line("  ⚠ " + msg)
            return
        if missing:
            self._log_line(f"  ⚠ Missing stems, continuing without: {', '.join(missing)}")
        return self._attach(gp, song_dir, label, parts, errors)

    def _attach(self, gp, song_dir, label: str, parts: list, errors: list):
        self._progress("gp", 0.5)
        self._log_line(f"▶ Attaching audio to {gp.name}")
        try:
            mixed = song_dir / f".{label}_audio.wav"
            gp_audio.mix_stems(parts, str(mixed))
            drum = next((p for p in (song_dir / f"{label} (drums).wav",
                                     song_dir / f"{label} (OD).wav") if p.exists()), None)
            gp_audio.align_and_embed(str(gp), str(drum) if drum else None,
                                     str(mixed), str(gp) + ".tmp", log=self._log_line)
            os.replace(str(gp) + ".tmp", str(gp))
            mixed.unlink(missing_ok=True)
            self._log_line(f"  ✓ Audio embedded ({len(parts)} stem(s))")
            self._progress("gp", 1.0)
        except Exception as e:
            errors.append(f"audio embed failed: {e}")
            self._log_line(f"  ✗ Audio embed failed: {e}")

    def _show_progress(self, stages):
        self._log.clear()
        self._log.reset_stages()
        self._log.show_stages(stages)
        if self._overlay is not None:
            self._overlay.show(len(stages))
        else:
            self._log.pack(fill="x", padx=12, pady=(0, 12))

    def _process_done(self, msg: str, errors=None):
        errors = list(errors or [])
        self._process_btn.configure(state="normal", text="Process")
        color = "#f06060" if errors else GREEN
        self._log_line(f"\n{'✗' if errors else '✓'} {msg}")
        for e in errors:
            self._log_line(f"   • {e}")
        if not errors:
            self._status.configure(text=msg, text_color=color)
            return
        # Saying only "2 errors" and hiding what they were is useless. Put the
        # first one in the status bar and keep the panel up with all of them.
        self._status.configure(text=f"{msg}  {errors[0]}", text_color=color)
        self._last_errors = errors
        if self._overlay is not None:
            try:
                self._overlay.show_errors(errors)
            except Exception:
                pass


if __name__ == "__main__":
    app = App()
    app.mainloop()
