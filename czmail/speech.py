"""Offline push-to-talk dictation with Vosk.

Audio comes from `arecord` (alsa-utils) as 16 kHz mono 16-bit PCM and is fed
to a Vosk recognizer while you speak, so text appears live and is ready the
moment you stop. Install with: sudo sh /usr/share/APPLaunch/apps/czmail/install_speech.sh
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading

RATE = 16000
CHUNK = 3200  # 0.1 s of audio

MODEL_DIRS = [
    "/usr/local/share/czmail/vosk-model",
    "/usr/share/czmail/vosk-model",
    os.path.join(os.path.expanduser("~"), ".local", "share", "czmail", "vosk-model"),
]

# Spoken punctuation -> text (Vosk outputs lower-case words, no punctuation)
SPOKEN = [
    (r"\s*\bnew paragraph\b\s*", "\n\n"),
    (r"\s*\bnew line\b\s*", "\n"),
    (r"\s*\b(full stop|period)\b", "."),
    (r"\s*\bcomma\b", ","),
    (r"\s*\bquestion mark\b", "?"),
    (r"\s*\bexclamation (mark|point)\b", "!"),
    (r"\s*\bcolon\b", ":"),
    (r"\s*\bsemicolon\b", ";"),
    (r"\bat sign\b", "@"),
]


def find_model():
    env = os.environ.get("CZMAIL_VOSK_MODEL")
    cands = ([env] if env else []) + MODEL_DIRS
    for base in cands:
        if not base:
            continue
        if os.path.isdir(os.path.join(base, "am")) or os.path.isfile(os.path.join(base, "conf", "model.conf")):
            return base
        # an unzipped "vosk-model-small-en-us-0.15" folder inside base
        for sub in sorted(glob.glob(os.path.join(base, "vosk-model*"))):
            if os.path.isdir(sub):
                return sub
    return None


def availability():
    """(ok, reason)."""
    try:
        import vosk  # noqa: F401
    except ImportError:
        return False, "Speech not installed. Run install_speech.sh (see README)"
    if not find_model():
        return False, "No Vosk model found. Run install_speech.sh"
    if not shutil.which("arecord"):
        return False, "arecord missing: sudo apt install alsa-utils"
    return True, ""


def tidy(text, before=""):
    """Turn recognizer words into text to insert after `before`."""
    t = " " + text.strip() + " "
    for pat, rep in SPOKEN:
        t = re.sub(pat, rep, t, flags=re.I)
    t = re.sub(r"[ \t]+", " ", t).strip(" ")
    t = re.sub(r" ([.,?!:;])", r"\1", t)
    if not t:
        return ""
    # Capitalise sentence starts (also after punctuation inside the dictation)
    prev = before.rstrip(" ")
    start = (not prev) or prev[-1] in ".?!\n"
    chars = list(t)
    cap = start
    for i, c in enumerate(chars):
        if cap and c.isalpha():
            chars[i] = c.upper()
            cap = False
        elif c in ".?!\n":
            cap = True
    t = "".join(chars)
    t = re.sub(r"\bi\b", "I", t)
    if before and not before[-1].isspace() and t[0] not in ".,?!:;\n":
        t = " " + t
    return t


class Dictation:
    """States: idle, loading, listening, finishing, done, error."""

    def __init__(self, device=None):
        self.device = device or os.environ.get("CZMAIL_MIC", "default")
        self.model = None
        self.state = "idle"
        self.text = ""
        self.partial = ""
        self.error = ""
        self.level = 0.0
        self.version = 0  # bumps whenever something visible changes
        self._stop = threading.Event()
        self._cancel = False
        self._lock = threading.Lock()
        self._thread = None

    @property
    def active(self):
        return self.state in ("loading", "listening", "finishing")

    def start(self):
        if self.active:
            return
        self.text = self.partial = self.error = ""
        self._stop.clear()
        self._cancel = False
        self._set("loading" if self.model is None else "listening")
        self._thread = threading.Thread(target=self._run, name="czmail-dictation", daemon=True)
        self._thread.start()

    def stop(self):
        if self.active:
            self._stop.set()
            if self.state == "listening":
                self._set("finishing")

    def cancel(self):
        self._cancel = True
        self._stop.set()
        self._set("idle")

    def take(self):
        """Return the final text once and go back to idle."""
        t = self.text
        self.text = ""
        self._set("idle")
        return t

    def _set(self, state):
        with self._lock:
            self.state = state
            self.version += 1

    def _run(self):
        proc = None
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            if self.model is None:
                path = find_model()
                if not path:
                    raise RuntimeError("No Vosk model found")
                self.model = Model(path)
                if self._cancel:
                    return
                self._set("listening")
            rec = KaldiRecognizer(self.model, RATE)
            proc = subprocess.Popen(
                ["arecord", "-q", "-D", self.device, "-f", "S16_LE", "-r", str(RATE),
                 "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            parts = []
            got_audio = False
            while not self._stop.is_set():
                data = proc.stdout.read(CHUNK)
                if not data:
                    err = proc.stderr.read().decode("utf-8", "replace").strip()
                    raise RuntimeError(err.splitlines()[-1] if err else "microphone stopped")
                got_audio = True
                self.level = _level(data)
                if rec.AcceptWaveform(data):
                    t = json.loads(rec.Result()).get("text", "")
                    if t:
                        parts.append(t)
                    self.partial = " ".join(parts)
                else:
                    p = json.loads(rec.PartialResult()).get("partial", "")
                    self.partial = " ".join(parts + ([p] if p else []))
                self.version += 1
            if self._cancel:
                return
            if got_audio:
                t = json.loads(rec.FinalResult()).get("text", "")
                if t:
                    parts.append(t)
            self.text = " ".join(parts).strip()
            self._set("done")
        except Exception as e:
            if not self._cancel:
                self.error = str(e) or e.__class__.__name__
                self._set("error")
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


def _level(data):
    """Rough 0..1 loudness of a 16-bit little-endian chunk."""
    n = len(data) // 2
    if not n:
        return 0.0
    step = max(1, n // 200)
    peak = 0
    for i in range(0, n, step):
        v = int.from_bytes(data[2 * i:2 * i + 2], "little", signed=True)
        if v < 0:
            v = -v
        if v > peak:
            peak = v
    return min(1.0, peak / 12000.0)
