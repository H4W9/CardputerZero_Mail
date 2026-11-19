#!/bin/sh
# Offline dictation for the Email app: installs Vosk and a small English model.
#   sudo sh /usr/share/APPLaunch/apps/czmail/install_speech.sh
# Needs internet once (~45 MB). Audio is never sent anywhere.
set -e

MODEL_NAME=vosk-model-small-en-us-0.15
MODEL_URL="https://alphacephei.com/vosk/models/$MODEL_NAME.zip"
DEST=/usr/local/share/czmail/vosk-model

if [ "$(id -u)" != 0 ]; then
    echo "Please run with sudo:  sudo sh $0"
    exit 1
fi

echo "== Packages (alsa-utils, pip, unzip)"
apt-get install -y alsa-utils python3-pip unzip curl

echo "== Vosk Python module"
if python3 -c "import vosk" 2>/dev/null; then
    echo "already installed"
else
    pip3 install --break-system-packages vosk 2>/dev/null || pip3 install vosk
fi

echo "== Speech model ($MODEL_NAME)"
if [ -d "$DEST/$MODEL_NAME" ]; then
    echo "already installed in $DEST"
else
    tmp=$(mktemp -d)
    curl -L --fail -o "$tmp/model.zip" "$MODEL_URL"
    mkdir -p "$DEST"
    unzip -q "$tmp/model.zip" -d "$DEST"
    rm -rf "$tmp"
    echo "installed to $DEST/$MODEL_NAME"
fi

echo "== Microphones"
if arecord -l 2>/dev/null | grep -q "^card"; then
    arecord -l
    echo
    echo "Quick test (speak for 3 s, then it plays back if a speaker exists):"
    echo "  arecord -d 3 -f S16_LE -r 16000 -c 1 /tmp/mic.wav && aplay /tmp/mic.wav"
    echo "If the wrong mic is used, set CZMAIL_MIC=plughw:<card>,<device> in"
    echo "/usr/share/APPLaunch/bin/czmail (before the exec line)."
else
    echo "WARNING: no capture device found (arecord -l is empty)."
    echo "Plug in a USB microphone/headset, then try dictation again."
fi

echo
echo "Done. In the Email app, press Ctrl+Space (or Fn+Space) while writing."
