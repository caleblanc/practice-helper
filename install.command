#!/bin/bash
#
# Practice Helper — macOS installer.
#
# Double-click this file. It will fetch the app if needed, build an isolated
# Python environment for it, and install "Practice Helper" into /Applications.
#
# Deliberately a bootstrap installer rather than a frozen bundle: the stem
# separator pulls in PyTorch, which is several gigabytes and notoriously
# fragile to freeze. Building the environment on the machine is both smaller
# to download and far more likely to actually work.

set -u
REPO="https://github.com/caleblanc/practice-helper"
APP_NAME="Practice Helper"
MIN_MINOR=11          # we need Python 3.11+

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
err()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

die() {
    err "$*"
    printf '\nInstallation stopped. Nothing was changed.\n'
    printf 'Press Return to close this window.\n'
    read -r _
    exit 1
}

cd "$(dirname "$0")" || die "Could not find my own folder."

say "Practice Helper installer"

# ── 1. Locate a suitable Python ───────────────────────────────────────────────
# Checked in preference order; the Homebrew and python.org paths are listed
# explicitly because a GUI double-click gets a minimal PATH.
PY=""
for cand in python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
            /usr/bin/python3; do
    p="$(command -v "$cand" 2>/dev/null || true)"
    [ -z "$p" ] && [ -x "$cand" ] && p="$cand"
    [ -z "$p" ] && continue
    minor="$("$p" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)"
    major="$("$p" -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)"
    if [ "$major" = "3" ] && [ "$minor" -ge "$MIN_MINOR" ] 2>/dev/null; then
        PY="$p"
        break
    fi
done

if [ -z "$PY" ]; then
    die "Python 3.$MIN_MINOR or newer is required.
    Install it from https://www.python.org/downloads/ (or 'brew install python')
    and then run this installer again."
fi
ok "Using $("$PY" --version 2>&1) at $PY"

# ── 2. Make sure we have the source ───────────────────────────────────────────
# The installer is designed to work on its own, so if it was downloaded by
# itself it fetches the app rather than failing.
if [ ! -f "app.py" ]; then
    say "Downloading Practice Helper"
    SRC="$HOME/Applications/Practice Helper (source)"
    if command -v git >/dev/null 2>&1; then
        rm -rf "$SRC"
        mkdir -p "$(dirname "$SRC")"
        git clone --depth 1 -q "$REPO" "$SRC" || die "Download failed. Check your internet connection."
    else
        TMPZ="$(mktemp -d)"
        curl -fsSL "$REPO/archive/refs/heads/main.zip" -o "$TMPZ/src.zip" \
            || die "Download failed. Check your internet connection."
        /usr/bin/unzip -q "$TMPZ/src.zip" -d "$TMPZ" || die "Could not unpack the download."
        rm -rf "$SRC"; mkdir -p "$(dirname "$SRC")"
        mv "$TMPZ"/practice-helper-* "$SRC"
        rm -rf "$TMPZ"
    fi
    cd "$SRC" || die "Could not enter $SRC"
    ok "Downloaded to $SRC"
fi
APP_SRC="$(pwd)"

# ── 3. Build the virtual environment ──────────────────────────────────────────
say "Setting up Python environment"
info "This installs PyTorch and can take several minutes on a first run."
if [ ! -x ".venv/bin/python" ]; then
    "$PY" -m venv .venv || die "Could not create the virtual environment."
fi
ok "Environment ready"

say "Installing dependencies"
./.venv/bin/python -m pip install --upgrade pip -q || die "Could not update pip."
if ! ./.venv/bin/python -m pip install -r requirements.txt; then
    die "Dependency installation failed. The messages above say why."
fi
ok "Dependencies installed"

# ── 4. Optional extra ─────────────────────────────────────────────────────────
# Downloader tools are no longer installed here. Which one you need depends on
# the streaming service you pick, and you pick that on first launch -- so the
# app asks for consent and installs it then, into this same environment.

# ffmpeg is optional on macOS (afconvert covers conversion) but demucs is
# happier with it, so mention it rather than silently proceeding.
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ffmpeg ]; then
    info "ffmpeg was not found. It is optional on macOS; 'brew install ffmpeg' adds it."
fi

# ── 5. Build the .app bundle ──────────────────────────────────────────────────
say "Creating $APP_NAME.app"
DEST="/Applications/$APP_NAME.app"
if [ ! -w /Applications ]; then
    DEST="$HOME/Applications/$APP_NAME.app"
    mkdir -p "$HOME/Applications"
    info "/Applications is not writable — installing to your home Applications folder."
fi

rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources"

cat > "$DEST/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>com.caleblanc.practicehelper</string>
  <key>CFBundleVersion</key><string>0.03</string>
  <key>CFBundleShortVersionString</key><string>0.03</string>
  <key>CFBundleExecutable</key><string>practice-helper</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

cat > "$DEST/Contents/MacOS/practice-helper" <<LAUNCHER
#!/bin/bash
cd "$APP_SRC" || exit 1
exec "$APP_SRC/.venv/bin/python" "$APP_SRC/app.py" "\$@"
LAUNCHER
chmod +x "$DEST/Contents/MacOS/practice-helper"

[ -f "assets/icon.icns" ] && cp "assets/icon.icns" "$DEST/Contents/Resources/icon.icns"

# Nudge Launch Services so the icon appears straight away rather than after a
# cache refresh some minutes later.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$DEST" >/dev/null 2>&1 || true
ok "Installed to $DEST"

say "Done"
info "Open \"$APP_NAME\" from Applications or Spotlight."
info "On first launch it will offer to set up a streaming service."
printf '\n'
read -r -p "Open it now? [Y/n] " reply
case "$reply" in
    [Nn]*) ;;
    *) open "$DEST" ;;
esac

printf '\nPress Return to close this window.\n'
read -r _
