"""Application core: worker thread, screen stack and all screens."""

import queue
import threading
import time

from PIL import Image, ImageDraw

from . import __version__, config, mailer, speech
from .contacts import ContactBook
from .keyboard import Key
from .ui import (C, H, W, draw_bar, draw_footer, ellipsize, font, text_w,
                 wrap_lines, wrap_offsets)

LONG_ESC = 0.8     # seconds held = exit app
PAGE = 30          # message headers fetched per page
TOAST_SECS = 2.8


# ------------------------------------------------------------ worker -----

class Worker:
    """Runs network jobs one at a time off the UI thread."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        t = threading.Thread(target=self._loop, name="czmail-worker", daemon=True)
        t.start()

    def submit(self, fn, done, fail):
        self.jobs.put((fn, done, fail))

    def _loop(self):
        while True:
            fn, done, fail = self.jobs.get()
            try:
                self.results.put((done, fn(), None))
            except Exception as e:  # reported on the UI thread
                self.results.put((fail, None, e))

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.results.get_nowait())
            except queue.Empty:
                return out


# ------------------------------------------------------------- widgets ---

def is_mic_key(ev):
    return ev.key == "mic" or (ev.ctrl and ev.char == " ")


class TextField:
    def __init__(self, value="", mask=False, placeholder=""):
        self.value = value
        self.cur = len(value)
        self.mask = mask
        self.scroll = 0
        self.placeholder = placeholder

    def insert(self, text):
        text = text.replace("\n", " ")
        self.value = self.value[:self.cur] + text + self.value[self.cur:]
        self.cur += len(text)

    def on_key(self, ev):
        """Returns True if the key was consumed."""
        v, c = self.value, self.cur
        if ev.char and not ev.ctrl:
            self.value = v[:c] + ev.char + v[c:]
            self.cur = c + len(ev.char)
        elif ev.key == "bs":
            if c > 0:
                self.value = v[:c - 1] + v[c:]
                self.cur = c - 1
        elif ev.key == "del":
            self.value = v[:c] + v[c + 1:]
        elif ev.key == "left":
            self.cur = max(0, c - 1)
        elif ev.key == "right":
            self.cur = min(len(v), c + 1)
        elif ev.key == "home":
            self.cur = 0
        elif ev.key == "end":
            self.cur = len(v)
        else:
            return False
        return True

    def draw(self, d, x, y, w, h, focus, fnt):
        d.rectangle([x, y, x + w - 1, y + h - 1],
                    fill=C["field_focus"] if focus else C["field"])
        shown = "•" * len(self.value) if self.mask else self.value
        cx = text_w(fnt, shown[:self.cur])
        inner = w - 6
        if cx - self.scroll > inner:
            self.scroll = cx - inner
        elif cx < self.scroll:
            self.scroll = max(0, cx - inner // 3)
        if not focus:
            self.scroll = 0
            shown = ellipsize(fnt, shown, inner)
        tx = x + 3 - self.scroll
        # Clip by drawing onto a strip.
        strip = Image.new("RGB", (w - 4, h - 2), C["field_focus"] if focus else C["field"])
        sd = ImageDraw.Draw(strip)
        if not shown and self.placeholder:
            sd.text((1, 0), ellipsize(fnt, self.placeholder, inner), font=fnt, fill=C["dim"])
        sd.text((tx - x - 2, 0), shown, font=fnt, fill=C["text"])
        if focus:
            px = tx - x - 2 + cx
            sd.line([px, 1, px, h - 4], fill=C["cursor"])
        d._image.paste(strip, (x + 2, y + 1))


class ChoiceField:
    """Cycles through fixed options with Left/Right/Space."""

    def __init__(self, value, options, labels):
        self.options = list(options)
        self.labels = labels
        self.value = value if value in self.options else self.options[0]

    def on_key(self, ev):
        i = self.options.index(self.value)
        if ev.key == "left":
            self.value = self.options[(i - 1) % len(self.options)]
        elif ev.key == "right" or ev.char == " ":
            self.value = self.options[(i + 1) % len(self.options)]
        else:
            return False
        return True

    def draw(self, d, x, y, w, h, focus, fnt):
        d.rectangle([x, y, x + w - 1, y + h - 1],
                    fill=C["field_focus"] if focus else C["field"])
        label = self.labels.get(self.value, self.value)
        d.text((x + 3, y), label, font=fnt, fill=C["text"])
        if focus:
            d.text((x + w - 30, y), "◀ ▶", font=fnt, fill=C["accent"])


class TextArea:
    """Multi-line editor with word wrap."""

    def __init__(self, value=""):
        self.value = value
        self.cur = 0
        self.top = 0
        self.width = W - 12
        self.fnt = font("regular", 11)
        self.lh = 13

    def _lines(self):
        return wrap_offsets(self.value, self.fnt, self.width)

    def _line_of(self, lines, pos):
        for i, (a, b) in enumerate(lines):
            nxt = lines[i + 1][0] if i + 1 < len(lines) else None
            if a <= pos <= b and (nxt is None or pos < nxt or pos == b):
                return i
        return len(lines) - 1

    def at_first_line(self):
        return self._line_of(self._lines(), self.cur) == 0

    def insert(self, text):
        self.value = self.value[:self.cur] + text + self.value[self.cur:]
        self.cur += len(text)

    def on_key(self, ev):
        v, c = self.value, self.cur
        if ev.char and not ev.ctrl:
            self.value = v[:c] + ev.char + v[c:]
            self.cur = c + len(ev.char)
        elif ev.key == "enter":
            self.value = v[:c] + "\n" + v[c:]
            self.cur = c + 1
        elif ev.key == "bs":
            if c > 0:
                self.value = v[:c - 1] + v[c:]
                self.cur = c - 1
        elif ev.key == "del":
            self.value = v[:c] + v[c + 1:]
        elif ev.key == "left":
            self.cur = max(0, c - 1)
        elif ev.key == "right":
            self.cur = min(len(v), c + 1)
        elif ev.key in ("up", "down", "pgup", "pgdn", "home", "end"):
            lines = self._lines()
            i = self._line_of(lines, c)
            a, b = lines[i]
            if ev.key == "home":
                self.cur = a
                return True
            if ev.key == "end":
                self.cur = b
                return True
            step = {"up": -1, "down": 1, "pgup": -6, "pgdn": 6}[ev.key]
            j = max(0, min(len(lines) - 1, i + step))
            if j == i:
                self.cur = 0 if step < 0 else len(v)
                return True
            x = text_w(self.fnt, v[a:c])
            ja, jb = lines[j]
            best = ja
            for k in range(ja, jb + 1):
                if text_w(self.fnt, v[ja:k]) <= x + 2:
                    best = k
                else:
                    break
            self.cur = best
        else:
            return False
        return True

    def draw(self, d, x, y, h, focus):
        lines = self._lines()
        rows = max(1, h // self.lh)
        ci = self._line_of(lines, self.cur)
        if ci < self.top:
            self.top = ci
        elif ci >= self.top + rows:
            self.top = ci - rows + 1
        for r in range(rows):
            i = self.top + r
            if i >= len(lines):
                break
            a, b = lines[i]
            ly = y + r * self.lh
            d.text((x, ly), self.value[a:b], font=self.fnt, fill=C["text"])
            if focus and i == ci:
                px = x + text_w(self.fnt, self.value[a:self.cur])
                d.line([px, ly + 1, px, ly + self.lh - 1], fill=C["cursor"])
        if not self.value and not focus:
            d.text((x, y), "Message…", font=self.fnt, fill=C["dim"])


# ------------------------------------------------------------- screens ---

class Screen:
    overlay = False
    typing = False  # True where letters are text, so H / ? are not shortcuts

    def __init__(self):
        self.app = None

    def on_show(self):
        pass

    def on_key(self, ev):
        pass

    def on_esc(self):
        self.app.pop()

    def draw(self, d):
        pass


class Confirm(Screen):
    overlay = True

    def __init__(self, question, on_yes, yes="Enter: Yes", no="Esc: No"):
        super().__init__()
        self.question, self.on_yes, self.yes, self.no = question, on_yes, yes, no

    def on_key(self, ev):
        if ev.key == "enter" or (ev.char or "").lower() == "y":
            self.app.pop()
            self.on_yes()
        elif (ev.char or "").lower() == "n":
            self.app.pop()

    def draw(self, d):
        x0, x1 = 30, W - 30
        y0 = (H - 72) // 2
        y1 = y0 + 72
        d.rectangle([x0, y0, x1, y1], fill=C["panel"], outline=C["accent"])
        f = font("bold", 11)
        lines = wrap_lines(self.question, f, x1 - x0 - 16)[:3]
        for i, line in enumerate(lines):
            d.text((x0 + 8, y0 + 8 + i * 14), line, font=f, fill=C["text"])
        fs = font("regular", 10)
        d.text((x0 + 8, y1 - 16), self.yes, font=fs, fill=C["ok"])
        d.text((x1 - 8 - text_w(fs, self.no), y1 - 16), self.no, font=fs, fill=C["warn"])


CHOICE_LABELS = {"text": "Text only", "full": "Full (formatted + images)",
                 "off": "Off", "on": "On (sender can see you opened it)",
                 "auto": "EXT display if connected", "builtin": "Built-in only"}

# (label, key, kind): kind is "text", "mask" or a tuple of options
SETUP_ROWS = [
    ("Email", "email", "text"),
    ("Password", "password", "mask"),
    ("Your name", "name", "text"),
    ("IMAP host", "imap_host", "text"),
    ("IMAP port", "imap_port", "text"),
    ("SMTP host", "smtp_host", "text"),
    ("SMTP port", "smtp_port", "text"),
    ("Login", "username", "text"),
    ("View mail", "view_mode", config.VIEW_MODES),
    ("Remote img", "remote_images", config.ON_OFF),
    ("Downloads", "download_dir", "text"),
    ("Screen", "screen", config.SCREENS),
]
CONNECTION_KEYS = ("email", "password", "imap_host", "imap_port", "smtp_host",
                   "smtp_port", "username")

SETUP_HINTS = {
    "email": "Type your address; servers fill in for common providers",
    "password": "Gmail/Yahoo/iCloud/Outlook: use an app password",
    "name": "Shown as the sender name (optional)",
    "imap_host": "Incoming server. Port 993 = SSL",
    "imap_port": "993 SSL, 143 STARTTLS",
    "smtp_host": "Outgoing server",
    "smtp_port": "465 SSL, 587 STARTTLS",
    "username": "Leave blank to log in with your email",
    "view_mode": "◀▶ or Space. V in the reader switches per message",
    "remote_images": "Web pictures; needs View mail = Full (or V in a message)",
    "download_dir": "Where attachments are saved",
    "screen": "DisplayMOD 2.8in screen. Takes effect next time you open Email",
}


def _make_field(acct, key, kind):
    if isinstance(kind, tuple):
        return ChoiceField(acct.get(key, ""), kind, CHOICE_LABELS)
    ph = config.default_download_dir() if key == "download_dir" else (
        "(your email)" if key == "username" else "")
    return TextField(acct.get(key, ""), kind == "mask", ph)


class SetupScreen(Screen):
    RH = 16
    typing = True

    def __init__(self, acct, first_run=False):
        super().__init__()
        self.first_run = first_run
        self.fields = [_make_field(acct, k, kind) for _, k, kind in SETUP_ROWS]
        self.focus = 0
        self.top = 0
        self.show_pw = False

    def _acct(self):
        a = config.empty_account()
        for (_, k, _), f in zip(SETUP_ROWS, self.fields):
            a[k] = f.value.strip() if k != "password" else f.value
        return a

    def _only_prefs_changed(self, a):
        cur = self.app.acct
        return config.is_complete(cur) and all(a[k] == cur.get(k, "") for k in CONNECTION_KEYS)

    def _move(self, delta):
        n = len(self.fields) + 1
        old = self.focus
        self.focus = (self.focus + delta) % n
        if old == 0 and self.focus != 0:
            self._autofill()

    def _autofill(self):
        a = self._acct()
        if config.apply_preset(a):
            for (_, k, _), f in zip(SETUP_ROWS, self.fields):
                if k in ("imap_host", "imap_port", "smtp_host", "smtp_port"):
                    f.value = a[k]
                    f.cur = len(f.value)
            self.app.toast("Server settings filled in", C["ok"])

    def on_key(self, ev):
        if ev.key in ("down", "tab"):
            self._move(1)
        elif ev.key == "up":
            self._move(-1)
        elif ev.key == "enter":
            if self.focus == len(self.fields):
                self._save()
            else:
                self._move(1)
        elif ev.ctrl and ev.char == "p":
            self.show_pw = not self.show_pw
            self.fields[1].mask = not self.show_pw
        elif ev.ctrl and ev.char == "s":
            self._save()
        elif self.focus < len(self.fields):
            self.fields[self.focus].on_key(ev)

    def on_esc(self):
        if self.first_run and not config.is_complete(self.app.acct):
            self.app.toast("Hold Esc to exit", C["dim"])
        else:
            self.app.pop()

    def _save(self):
        self._autofill()
        a = self._acct()
        missing = [lbl for lbl, k, _ in SETUP_ROWS
                   if k in ("email", "password", "imap_host", "smtp_host") and not a[k]]
        if missing:
            self.app.toast("Missing: " + ", ".join(missing), C["warn"])
            return
        for k in ("imap_port", "smtp_port"):
            if not a[k].isdigit():
                self.app.toast("Port must be a number", C["warn"])
                return
        if self._only_prefs_changed(a):
            self._commit(a, reconnect=False)
            self.app.toast("Settings saved", C["ok"])
            return

        def test():
            c = self.app.make_client(a)
            try:
                c.connect()
            finally:
                c.close()
            self.app.send_fn(a, None, test_only=True)

        def ok(_):
            self._commit(a)
            self.app.toast("Connected. Settings saved", C["ok"])

        def fail(e):
            def keep():
                self._commit(a)
                self.app.toast("Saved without a working connection", C["warn"])
            self.app.push(Confirm("%s\nSave anyway?" % e, keep))

        self.app.job("Testing…", test, ok, fail)

    def _commit(self, a, reconnect=True):
        self.app.set_account(a, reconnect)
        if self.first_run:
            self.app.replace(InboxScreen())
        else:
            self.app.pop()
            inbox = self.app.find(InboxScreen)
            if inbox and reconnect:
                inbox.refresh(reset=True)

    def draw(self, d):
        draw_bar(d, "Account setup")
        fl = font("regular", 10)
        ff = font("regular", 11)
        rows = (H - 16 - 13) // self.RH
        n = len(self.fields) + 1
        if self.focus < self.top:
            self.top = self.focus
        elif self.focus >= self.top + rows:
            self.top = self.focus - rows + 1
        for r in range(rows):
            i = self.top + r
            if i >= n:
                break
            y = 17 + r * self.RH
            if i == len(self.fields):
                focus = self.focus == i
                bx0, bx1 = 76, 200
                d.rectangle([bx0, y, bx1, y + self.RH - 2],
                            fill=C["bar"] if focus else C["field"],
                            outline=C["accent"] if focus else None)
                lbl = "Save & test"
                fb = font("bold", 11)
                d.text(((bx0 + bx1 - text_w(fb, lbl)) / 2, y + 1), lbl, font=fb,
                       fill=C["bar_text"])
                continue
            label = SETUP_ROWS[i][0]
            d.text((4, y + 2), label, font=fl,
                   fill=C["accent"] if self.focus == i else C["dim"])
            self.fields[i].draw(d, 72, y, W - 76, self.RH - 2, self.focus == i, ff)
        if self.focus < len(self.fields):
            hint = SETUP_HINTS.get(SETUP_ROWS[self.focus][1], "")
            if SETUP_ROWS[self.focus][1] == "password":
                hint += "  Ctrl+P: show"
        else:
            hint = "Enter: connect and save   Esc: back"
        draw_footer(d, hint)


class InboxScreen(Screen):
    RH = 28

    def __init__(self, folder="INBOX", label="Inbox", query=None, all_mail=False):
        super().__init__()
        self.folder, self.label = folder, label
        self.query, self.all_mail = query, all_mail
        self.gen = 0  # bumps on folder change so stale results are dropped
        self.uids = []
        self.items = []
        self.covered = 0  # how many of the newest uids have been fetched
        self.sel = 0
        self.top = 0
        self.loaded = False
        self.loading = False

    def on_show(self):
        if not self.loaded and not self.loading:
            self.refresh(reset=True)

    def set_folder(self, folder, label):
        self.folder, self.label = folder, label
        self.gen += 1
        self.refresh(reset=True)

    def refresh(self, reset=False):
        folder, gen, query, all_mail = self.folder, self.gen, self.query, self.all_mail
        if reset:
            self.items, self.uids, self.covered, self.sel, self.top = [], [], 0, 0, 0
            self.loaded = False
        self.loading = True

        def fetch():
            cl = self.app.client()
            if query:
                used, uids = cl.search(folder, query, all_mail)
            else:
                used, uids = folder, cl.search_uids(folder)
            page = uids[-PAGE:]
            return used, uids, cl.summaries(used, page), len(page)

        def done(r):
            self.loading = False
            if gen != self.gen:
                return
            self.folder, self.uids, self.items, self.covered = r
            self.loaded = True
            self.sel = min(self.sel, max(0, len(self.items) - 1))
            self.app.contacts.learn_received(self.items)
            self.app.sync_contacts()

        def fail(e):
            self.loading = False
            self.loaded = True
            self.app.toast(str(e), C["warn"])

        self.app.job("Searching…" if query else "Checking mail…", fetch, done, fail)

    def load_more(self):
        if self.loading or self.covered >= len(self.uids):
            return
        folder, gen = self.folder, self.gen
        end = len(self.uids) - self.covered
        chunk = self.uids[max(0, end - PAGE):end]
        self.loading = True

        def fetch():
            return self.app.client().summaries(folder, chunk)

        def done(sums):
            self.loading = False
            if gen != self.gen:
                return
            self.items.extend(sums)
            self.covered += len(chunk)
            self.app.contacts.learn_received(sums)

        def fail(e):
            self.loading = False
            self.app.toast(str(e), C["warn"])

        self.app.job("Loading older…", fetch, done, fail)

    def current(self):
        if 0 <= self.sel < len(self.items):
            return self.items[self.sel]
        return None

    def remove(self, uid):
        self.items = [s for s in self.items if s.uid != uid]
        self.uids = [u for u in self.uids if u != uid]
        self.covered = max(0, self.covered - 1)
        self.sel = min(self.sel, max(0, len(self.items) - 1))

    def on_key(self, ev):
        k, ch = ev.key, (ev.char or "")
        n = len(self.items)
        if k == "down" or ch == "j":
            if self.sel < n - 1:
                self.sel += 1
            if self.sel >= n - 3:
                self.load_more()
        elif k == "up" or ch == "k":
            self.sel = max(0, self.sel - 1)
        elif k == "pgdn" or ch == " ":
            self.sel = min(max(0, n - 1), self.sel + self.rows())
            if self.sel >= n - 3:
                self.load_more()
        elif k == "pgup":
            self.sel = max(0, self.sel - self.rows())
        elif k == "home":
            self.sel = 0
        elif k == "end":
            self.sel = max(0, n - 1)
        elif k == "enter" or k == "right":
            s = self.current()
            if s:
                self.app.push(ReaderScreen(self, s))
        elif ch in ("r", "R"):
            self.refresh()
        elif ch in ("c", "C", "n"):
            self.app.push(ComposeScreen())
        elif ch in ("f", "F", "g") and not ev.ctrl:
            self.app.push(FolderScreen(self))
        elif ch == "/" or (ev.ctrl and ch in ("f", "F")):
            self.app.push(SearchPrompt(self))
        elif ch in ("s", "S"):
            self.app.push(SetupScreen(self.app.acct))
        elif ch in ("u", "U"):
            s = self.current()
            if s:
                self.app.toggle_seen(self.folder, s)
        elif ch in ("d", "D") or k == "del":
            s = self.current()
            if s:
                self.app.push(Confirm("Delete \"%s\"?" % s.subject[:60],
                                      lambda: self.app.delete(self, s)))

    def on_esc(self):
        if self.query:
            self.app.pop()
        elif self.folder.upper() != "INBOX":
            self.set_folder("INBOX", "Inbox")
        else:
            self.app.toast("Hold Esc to exit", C["dim"])

    def rows(self):
        return (H - 16 - 13) // self.RH

    def draw(self, d):
        unread = sum(1 for s in self.items if not s.seen)
        if self.query:
            right = "%d found" % len(self.uids) if self.loaded else ""
        else:
            right = ("%d unread" % unread) if unread else ("%d" % len(self.uids) if self.uids else "")
        draw_bar(d, self.label, right)
        rows = self.rows()
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        fb, fr, fs = font("bold", 11), font("regular", 11), font("regular", 10)
        if not self.items:
            msg = ("Searching…" if self.query else "Loading…") \
                if (self.loading or not self.loaded) else ("No results" if self.query else "No messages")
            d.text(((W - text_w(fr, msg)) / 2, H // 2 - 13), msg, font=fr, fill=C["dim"])
        for r in range(rows):
            i = self.top + r
            if i >= len(self.items):
                break
            s = self.items[i]
            y = 16 + r * self.RH
            if i == self.sel:
                d.rectangle([0, y, W, y + self.RH - 1], fill=C["sel"])
            if not s.seen:
                d.ellipse([3, y + 6, 8, y + 11], fill=C["unread"])
            date = mailer.short_date(s.date)
            dw = text_w(fs, date)
            d.text((W - 6 - dw, y + 3), date, font=fs, fill=C["dim"])
            sf = fb if not s.seen else fr
            d.text((12, y + 1), ellipsize(sf, s.sender or s.sender_addr or "?", W - 26 - dw),
                   font=sf, fill=C["text"] if not s.seen else C["dim"] if i != self.sel else C["text"])
            d.text((12, y + 14), ellipsize(fs, s.subject, W - 20), font=fs,
                   fill=C["text"] if i == self.sel or not s.seen else C["dim"])
            d.line([12, y + self.RH - 1, W - 4, y + self.RH - 1], fill=C["panel"])
        if self.items and len(self.items) > rows:
            total = max(len(self.uids), len(self.items))
            th = max(6, int((H - 29) * rows / total))
            ty = 16 + int((H - 29 - th) * self.top / max(1, len(self.items) - rows))
            d.rectangle([W - 2, ty, W - 1, ty + th], fill=C["accent"])
        draw_footer(d, "Enter:open  /:search  C:new  R:refresh  F:folders  D:del  U:unread  S:setup"
                    if not self.query else "Enter:open  /:new search  D:del  U:unread  Esc:back")


class FolderScreen(Screen):
    RH = 15

    def __init__(self, inbox):
        super().__init__()
        self.inbox = inbox
        self.folders = []
        self.sel = 0
        self.top = 0
        self.loading = True

    def on_show(self):
        if self.folders or not self.loading:
            return

        def done(fs):
            self.loading = False
            self.folders = fs
            for i, (raw, _) in enumerate(fs):
                if raw == self.inbox.folder:
                    self.sel = i

        def fail(e):
            self.loading = False
            self.app.toast(str(e), C["warn"])

        self.app.job("Folders…", lambda: self.app.client().folders(), done, fail)

    def on_key(self, ev):
        n = len(self.folders)
        if ev.key == "down" or ev.char == "j":
            self.sel = min(max(0, n - 1), self.sel + 1)
        elif ev.key == "up" or ev.char == "k":
            self.sel = max(0, self.sel - 1)
        elif ev.key in ("enter", "right") and n:
            raw, label = self.folders[self.sel]
            self.app.pop()
            self.inbox.set_folder(raw, "Inbox" if raw.upper() == "INBOX" else label)

    def draw(self, d):
        draw_bar(d, "Folders")
        f = font("regular", 11)
        rows = (H - 16 - 13) // self.RH
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        if not self.folders:
            msg = "Loading…" if self.loading else "No folders"
            d.text(((W - text_w(f, msg)) / 2, H // 2 - 13), msg, font=f, fill=C["dim"])
        for r in range(rows):
            i = self.top + r
            if i >= len(self.folders):
                break
            y = 17 + r * self.RH
            if i == self.sel:
                d.rectangle([0, y - 1, W, y + self.RH - 2], fill=C["sel"])
            d.text((8, y), ellipsize(f, self.folders[i][1], W - 16), font=f, fill=C["text"])
        draw_footer(d, "Enter: open folder   Esc: back")


class DocView(Screen):
    """Scrollable document of text lines and images (reader, web view)."""
    LH = 13
    STEP = 13

    def __init__(self):
        super().__init__()
        self.items = []  # (kind, payload, height): ('line', (text, font, color)) | ('img', image)
        self.total = 0
        self.top = 0     # pixel scroll offset

    def _set_items(self, items):
        self.items = items
        self.total = sum(h for _, _, h in items)
        self.top = min(self.top, self.maxtop())

    def view_h(self):
        return H - 16 - 13

    def maxtop(self):
        return max(0, self.total - self.view_h())

    def add_text(self, out, text, f, color):
        for line in wrap_lines(text, f, W - 10):
            out.append(("line", (line, f, color), self.LH))

    def add_blocks(self, out, blocks):
        fb, fr, fs = font("bold", 11), font("regular", 11), font("regular", 10)
        styles = {"normal": (fr, C["text"]), "bold": (fb, C["text"]),
                  "dim": (fr, C["dim"]), "link": (fs, C["accent"])}
        for b in blocks:
            if b[0] == "img":
                out.append(("img", b[1], b[1].height + 4))
            else:
                f, color = styles.get(b[2], styles["normal"])
                self.add_text(out, b[1], f, color)
                out.append(("line", ("", fs, C["dim"]), 5))

    def scroll_key(self, ev):
        k, ch = ev.key, (ev.char or "")
        page = self.view_h() - self.LH
        if k == "down" or ch == "j":
            self.top = min(self.maxtop(), self.top + self.STEP)
        elif k == "up" or ch == "k":
            self.top = max(0, self.top - self.STEP)
        elif k == "pgdn" or ch == " ":
            self.top = min(self.maxtop(), self.top + page)
        elif k == "pgup" or ch == "b":
            self.top = max(0, self.top - page)
        elif k == "home":
            self.top = 0
        elif k == "end":
            self.top = self.maxtop()
        else:
            return False
        return True

    def draw_items(self, d):
        y0, vh = 16, self.view_h()
        y = -self.top
        for kind, payload, h in self.items:
            if y + h <= 0:
                y += h
                continue
            if y >= vh:
                break
            if kind == "line":
                text, f, color = payload
                if text and y >= -2:
                    d.text((5, y0 + 1 + y), text, font=f, fill=color)
            else:
                im = payload
                top_cut = max(0, -y)
                bot_cut = max(0, y + im.height - vh)
                if im.height - top_cut - bot_cut > 0:
                    part = im.crop((0, top_cut, im.width, im.height - bot_cut))
                    d._image.paste(part, ((W - im.width) // 2, y0 + 2 + y + top_cut))
            y += h
        if self.total > vh:
            th = max(6, int(vh * vh / self.total))
            ty = y0 + int((vh - th) * self.top / max(1, self.maxtop()))
            d.rectangle([W - 2, ty, W - 1, ty + th], fill=C["accent"])

    def draw_loading(self, d, title):
        f = font("regular", 11)
        d.text((5, 20), ellipsize(font("bold", 11), title, W - 10),
               font=font("bold", 11), fill=C["text"])
        d.text(((W - text_w(f, "Loading…")) / 2, H // 2 - 13), "Loading…", font=f, fill=C["dim"])


class ReaderScreen(DocView):
    def __init__(self, inbox, summary):
        super().__init__()
        self.inbox = inbox
        self.summary = summary
        self.msg = None
        self.started = False
        self.mode = None
        self.after_load = None

    def on_show(self):
        if self.started:
            return
        self.started = True
        self.mode = self.app.acct.get("view_mode", "text")
        self.load()

    def load(self):
        folder, uid = self.inbox.folder, self.summary.uid
        full = self.mode == "full"
        remote = self.app.acct.get("remote_images") == "on"
        self.items, self.total, self.top, self.msg = [], 0, 0, None

        def done(m):
            self.msg = m
            self.summary.seen = True
            self._layout()
            if self.after_load:
                fn, self.after_load = self.after_load, None
                fn()

        def fail(e):
            self._set_items([("line", (str(e), font("regular", 11), C["warn"]), self.LH)])

        self.app.job("Opening…",
                     lambda: self.app.client().message(folder, uid, full=full, remote=remote),
                     done, fail)

    def _layout(self):
        m = self.msg
        fb, fr, fs = font("bold", 11), font("regular", 11), font("regular", 10)
        out = []
        self.add_text(out, m.subject or "(no subject)", fb, C["text"])
        self.add_text(out, "From: " + m.sender, fs, C["dim"])
        if m.to:
            self.add_text(out, "To: " + m.to, fs, C["dim"])
        if m.cc:
            self.add_text(out, "Cc: " + m.cc, fs, C["dim"])
        if m.attachments:
            self.add_text(out, "Attached (T): " + ", ".join(a.name for a in m.attachments),
                          fs, C["accent"])
        if not m.full and m.image_count:
            self.add_text(out, "%d image%s hidden, press V to see them"
                          % (m.image_count, "s" if m.image_count != 1 else ""), fs, C["unread"])
        if m.links:
            self.add_text(out, "%d link%s, press L to open"
                          % (len(m.links), "s" if len(m.links) != 1 else ""), fs, C["accent"])
        out.append(("line", ("", fs, C["dim"]), 6))
        if m.full and m.blocks is not None:
            self.add_blocks(out, m.blocks)
        else:
            for line in wrap_lines(m.text or "(empty message)", fr, W - 10):
                color = C["dim"] if line.startswith(">") else C["text"]
                out.append(("line", (line, fr, color), self.LH))
        if m.truncated:
            out.append(("line", ("", fs, C["dim"]), 6))
            self.add_text(out, "[message too large to show completely]", fs, C["warn"])
        self.top = 0
        self._set_items(out)

    def open_images(self):
        if self.msg.images:
            self.app.push(ImagesScreen(self.msg.images))
        else:
            self.app.toast("No images in this message", C["dim"])

    def on_key(self, ev):
        k, ch = ev.key, (ev.char or "")
        if self.scroll_key(ev):
            return
        if k == "left":
            self.app.pop()
        elif ch in ("v", "V"):
            self.mode = "text" if self.mode == "full" else "full"
            self.app.toast("Full view" if self.mode == "full" else "Text only", C["accent"])
            self.load()
        elif self.msg is None:
            return
        elif ch in ("r", "R"):
            to, cc, subj, body = mailer.reply_fields(self.app.acct, self.msg)
            self.app.push(ComposeScreen(to, cc, subj, body, self.msg, "Reply"))
        elif ch in ("a", "A"):
            to, cc, subj, body = mailer.reply_fields(self.app.acct, self.msg, reply_all=True)
            self.app.push(ComposeScreen(to, cc, subj, body, self.msg, "Reply all"))
        elif ch in ("f", "F"):
            to, cc, subj, body = mailer.forward_fields(self.msg)
            self.app.push(ComposeScreen(to, cc, subj, body, None, "Forward"))
        elif ch in ("t", "T"):
            if self.msg.attachments:
                self.app.push(AttachmentsScreen(self))
            else:
                self.app.toast("No attachments", C["dim"])
        elif ch in ("i", "I"):
            if self.mode == "full":
                self.open_images()
            elif not self.msg.image_count:
                self.app.toast("No images in this message", C["dim"])
            else:  # images are only fetched in Full view: switch, then open the list
                self.mode = "full"
                self.after_load = self.open_images
                self.load()
        elif ch in ("l", "L"):
            if self.msg.links:
                self.app.push(LinksScreen(self.msg.links, self.msg.link_labels))
            else:
                self.app.toast("No links in this message", C["dim"])
        elif ch in ("u", "U"):
            self.app.toggle_seen(self.inbox.folder, self.summary)
            self.app.pop()
        elif ch in ("d", "D") or k == "del":
            def yes():
                self.app.pop()
                self.app.delete(self.inbox, self.summary)
            self.app.push(Confirm("Delete this message?", yes))

    def draw(self, d):
        s = self.summary
        tag = "  FULL" if self.mode == "full" else ""
        draw_bar(d, s.sender or s.sender_addr, mailer.short_date(s.date) + tag)
        if not self.items:
            self.draw_loading(d, s.subject)
        self.draw_items(d)
        draw_footer(d, "R:reply A:all F:fwd T:attach I:images L:links V:view D:del")


class ImagesScreen(Screen):
    """Images of a message or page, with thumbnails; save originals."""
    RH = 38

    def __init__(self, images):
        super().__init__()
        self.images = images
        self.sel = 0
        self.top = 0
        self.saved = set()
        self.thumbs = {}

    def thumb(self, i):
        t = self.thumbs.get(i)
        if t is None:
            from PIL import Image as _I
            ref = self.images[i]
            t = mailer.decode_image(ref.data, 48, 34) or _I.new("RGB", (48, 34), C["field"])
            self.thumbs[i] = t
        return t

    def on_key(self, ev):
        ch = ev.char or ""
        n = len(self.images)
        if ev.key == "down" or ch == "j":
            self.sel = min(n - 1, self.sel + 1)
        elif ev.key == "up" or ch == "k":
            self.sel = max(0, self.sel - 1)
        elif ev.key == "enter" or ch in ("s", "S"):
            self.save([self.sel])
        elif ch in ("a", "A"):
            self.save(list(range(n)))

    def save(self, idxs):
        dest = config.download_dir(self.app.acct)
        refs = [(i, self.images[i]) for i in idxs]

        def work():
            return [(i, mailer.save_bytes(r.data, dest, r.name)) for i, r in refs]

        def done(paths):
            self.saved.update(i for i, _ in paths)
            self.app.toast(("Saved %d images to %s" % (len(paths), dest)) if len(paths) > 1
                           else "Saved: " + paths[0][1], C["ok"])

        self.app.job("Saving…", work, done)

    def draw(self, d):
        draw_bar(d, "Images", "%d" % len(self.images))
        rows = (H - 16 - 13) // self.RH
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        fb, fs = font("bold", 11), font("regular", 10)
        for r in range(rows):
            i = self.top + r
            if i >= len(self.images):
                break
            ref = self.images[i]
            y = 16 + r * self.RH
            if i == self.sel:
                d.rectangle([0, y, W, y + self.RH - 1], fill=C["sel"])
            t = self.thumb(i)
            d._image.paste(t, (6 + (48 - t.width) // 2, y + 2 + (34 - t.height) // 2))
            d.text((60, y + 3), ellipsize(fb, ref.name, W - 66), font=fb, fill=C["text"])
            info = "%s  ·  %d×%d shown" % (mailer.human_size(len(ref.data)), ref.size[0], ref.size[1])
            if i in self.saved:
                info = "✓ saved  " + info
            d.text((60, y + 18), ellipsize(fs, info, W - 66), font=fs,
                   fill=C["ok"] if i in self.saved else C["dim"])
        draw_footer(d, "Enter: save original  A: save all  → " + config.download_dir(self.app.acct))


class LinksScreen(Screen):
    """Links in a message or page: open in web view, QR code, or download."""
    RH = 24

    def __init__(self, links, labels=None):
        super().__init__()
        self.links = links
        self.labels = labels or {}
        self.sel = 0
        self.top = 0

    def current(self):
        return self.links[self.sel] if self.links else None

    def on_key(self, ev):
        ch = (ev.char or "").lower()
        n = len(self.links)
        url = self.current()
        if ev.key == "down" or ch == "j":
            self.sel = min(n - 1, self.sel + 1)
        elif ev.key == "up" or ch == "k":
            self.sel = max(0, self.sel - 1)
        elif url is None:
            return
        elif ev.key in ("enter", "right") or ch == "o":
            self.app.push(WebScreen(url))
        elif ch == "q":
            self.app.push(QRScreen(url))
        elif ch == "s":
            self.app.download_link(url)

    def draw(self, d):
        draw_bar(d, "Links", "%d" % len(self.links))
        rows = (H - 16 - 13) // self.RH
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        fb, fs = font("bold", 11), font("regular", 10)
        for r in range(rows):
            i = self.top + r
            if i >= len(self.links):
                break
            url = self.links[i]
            y = 16 + r * self.RH
            if i == self.sel:
                d.rectangle([0, y, W, y + self.RH - 1], fill=C["sel"])
            label = self.labels.get(url) or mailer.urlparse(url).netloc
            d.text((6, y + 1), ellipsize(fb, "%d. %s" % (i + 1, label), W - 12), font=fb, fill=C["text"])
            d.text((6, y + 12), ellipsize(fs, url, W - 12), font=fs, fill=C["accent"])
        draw_footer(d, "Enter: open  Q: QR code for phone  S: download  Esc: back")


class WebScreen(DocView):
    """Minimal text web view for a link."""

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.page = None
        self.error = ""
        self.started = False

    def on_show(self):
        if self.started:
            return
        self.started = True
        remote = self.app.acct.get("remote_images") == "on"

        def done(pg):
            self.page = pg
            self._layout()

        def fail(e):
            self.error = str(getattr(e, "reason", None) or e)
            mailer.log("open link failed (%s): %s" % (self.error, self.url[:200]))
            out = []
            self.add_text(out, "Could not open this link", font("bold", 11), C["warn"])
            self.add_text(out, self.error, font("regular", 10), C["dim"])
            self.add_text(out, "Q shows a QR code to open it on your phone.", font("regular", 10), C["dim"])
            self._set_items(out)

        self.app.job("Loading page…", lambda: mailer.fetch_page(self.url, remote), done, fail)

    def _layout(self):
        pg = self.page
        fb, fs = font("bold", 11), font("regular", 10)
        out = []
        if pg.title:
            self.add_text(out, pg.title, fb, C["text"])
        self.add_text(out, pg.final, fs, C["accent"])
        if pg.links:
            self.add_text(out, "%d links, press L to follow" % len(pg.links), fs, C["dim"])
        out.append(("line", ("", fs, C["dim"]), 6))
        if pg.kind in ("html", "text", "image"):
            self.add_blocks(out, pg.blocks)
            if pg.truncated:
                self.add_text(out, "[page cut off at 4 MB]", fs, C["warn"])
        else:
            self.add_text(out, "This link is a file", fb, C["text"])
            self.add_text(out, "%s  ·  %s" % (pg.filename, pg.ctype or "unknown type"), fs, C["dim"])
            self.add_text(out, "Size: %s" % mailer.human_size(pg.size), fs, C["dim"])
            self.add_text(out, "Press S to save it to Downloads.", fs, C["ok"])
        self.top = 0
        self._set_items(out)

    def on_key(self, ev):
        if self.scroll_key(ev):
            return
        ch = (ev.char or "").lower()
        pg = self.page
        if ev.key == "left":
            self.app.pop()
        elif ch == "q":
            self.app.push(QRScreen(pg.final if pg else self.url))
        elif ch == "s":
            self.app.download_link(pg.final if pg else self.url)
        elif pg is None:
            return
        elif ch == "l":
            if pg.links:
                self.app.push(LinksScreen(pg.links, pg.labels))
            else:
                self.app.toast("No links on this page", C["dim"])
        elif ch == "i":
            if pg.images:
                self.app.push(ImagesScreen(pg.images))
            else:
                self.app.toast("No images loaded (Remote img setting)", C["dim"])

    def draw(self, d):
        host = mailer.urlparse(self.page.final if self.page else self.url).netloc
        draw_bar(d, host or "Web")
        if not self.items:
            self.draw_loading(d, self.url)
        self.draw_items(d)
        draw_footer(d, "L:links  I:images  S:save  Q:QR for phone  Esc:back")


class QRScreen(Screen):
    """Show a URL as a QR code so a phone can open it."""

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.img = None
        self.error = ""
        try:
            import qrcode
            from qrcode.constants import ERROR_CORRECT_L
            q = qrcode.QRCode(error_correction=ERROR_CORRECT_L, border=2, box_size=1)
            q.add_data(url)
            q.make(fit=True)
            self.matrix = q.get_matrix()
        except ImportError:
            self.matrix = None
            self.error = "QR codes need python3-qrcode:\nsudo apt install python3-qrcode"
        except Exception as e:
            self.matrix = None
            self.error = "Link too long for a QR code (%s)" % e

    def on_key(self, ev):
        if ev.key in ("enter", "left"):
            self.app.pop()

    def draw(self, d):
        d.rectangle([0, 0, W, H], fill=(255, 255, 255))
        fs = font("regular", 10)
        if self.matrix is None:
            d.rectangle([0, 0, W, H], fill=C["bg"])
            draw_bar(d, "QR code")
            for i, line in enumerate(self.error.split("\n")):
                d.text((10, 40 + i * 14), line, font=font("regular", 11), fill=C["warn"])
            return
        n = len(self.matrix)
        scale = max(1, min(H - 4, 166) // n)
        size = n * scale
        x0, y0 = 4, (H - size) // 2
        for r, row in enumerate(self.matrix):
            for c, on in enumerate(row):
                if on:
                    d.rectangle([x0 + c * scale, y0 + r * scale,
                                 x0 + (c + 1) * scale - 1, y0 + (r + 1) * scale - 1], fill=(0, 0, 0))
        tx = x0 + size + 8
        tw = W - tx - 4
        d.text((tx, 8), "Scan with", font=font("bold", 11), fill=(0, 0, 0))
        d.text((tx, 22), "your phone", font=font("bold", 11), fill=(0, 0, 0))
        lines = wrap_lines(self.url, fs, tw)
        for i, line in enumerate(lines[:9]):
            d.text((tx, 44 + i * 12), line, font=fs, fill=(70, 70, 70))
        if len(lines) > 9:
            d.text((tx, 44 + 9 * 12), "…", font=fs, fill=(70, 70, 70))
        if scale == 1 and n > 100:
            d.text((tx, H - 14), "dense: hold phone close", font=fs, fill=(180, 60, 40))


class SearchPrompt(Screen):
    """Overlay asking for a search query and scope."""
    overlay = True
    typing = True

    def __init__(self, inbox):
        super().__init__()
        self.inbox = inbox
        self.query = TextField("", placeholder="words, sender, subject…")
        self.scope = ChoiceField("folder", ("folder", "all"),
                                 {"folder": "This folder (%s)" % inbox.label, "all": "All mail"})
        self.focus = 0

    def on_key(self, ev):
        if ev.key in ("tab", "down", "up"):
            self.focus = 1 - self.focus
        elif ev.key == "enter":
            q = self.query.value.strip()
            if not q:
                self.app.toast("Type something to search for", C["warn"])
                return
            self.app.pop()
            self.app.push(InboxScreen(self.inbox.folder, "Search: " + q, query=q,
                                      all_mail=self.scope.value == "all"))
        elif self.focus == 0:
            self.query.on_key(ev)
        else:
            self.scope.on_key(ev)

    def draw(self, d):
        x0, x1 = 14, W - 14
        y0 = (H - 98) // 2
        y1 = y0 + 98
        d.rectangle([x0, y0, x1, y1], fill=C["panel"], outline=C["accent"])
        fb, fs, ff = font("bold", 11), font("regular", 10), font("regular", 11)
        d.text((x0 + 8, y0 + 6), "Search mail", font=fb, fill=C["text"])
        self.query.draw(d, x0 + 8, y0 + 24, x1 - x0 - 16, 16, self.focus == 0, ff)
        d.text((x0 + 8, y0 + 48), "In:", font=fs, fill=C["accent"] if self.focus == 1 else C["dim"])
        self.scope.draw(d, x0 + 30, y0 + 46, x1 - x0 - 38, 15, self.focus == 1, fs)
        tip = "Gmail: from:, subject:, has:attachment work" if "gmail" in \
            self.app.acct.get("imap_host", "") else "Matches sender, subject and text"
        d.text((x0 + 8, y0 + 68), tip, font=fs, fill=C["dim"])
        d.text((x0 + 8, y0 + 82), "Enter: search   Tab: scope   Esc: cancel", font=fs, fill=C["dim"])


class AttachmentsScreen(Screen):
    RH = 26

    def __init__(self, reader):
        super().__init__()
        self.reader = reader
        self.sel = 0
        self.top = 0
        self.saved = {}

    def on_key(self, ev):
        atts = self.reader.msg.attachments
        ch = ev.char or ""
        if ev.key == "down" or ch == "j":
            self.sel = min(len(atts) - 1, self.sel + 1)
        elif ev.key == "up" or ch == "k":
            self.sel = max(0, self.sel - 1)
        elif ev.key == "enter" or ch in ("s", "S"):
            self.save([atts[self.sel]])
        elif ch in ("a", "A"):
            self.save(list(atts))

    def save(self, atts):
        msg = self.reader.msg
        folder, uid = self.reader.inbox.folder, msg.uid
        dest = config.download_dir(self.app.acct)

        def work():
            raw = msg.raw
            if raw is None:
                raw = self.app.client().fetch_raw(folder, uid)
                msg.raw = raw
            return [(a.idx, mailer.save_attachment(raw, a.idx, dest, a.name)) for a in atts]

        def done(paths):
            for idx, p in paths:
                self.saved[idx] = p
            last = paths[-1][1] if paths else dest
            self.app.toast("Saved %d to %s" % (len(paths), last) if len(paths) > 1
                           else "Saved: " + last, C["ok"])

        self.app.job("Downloading…", work, done)

    def draw(self, d):
        atts = self.reader.msg.attachments
        draw_bar(d, "Attachments", "%d" % len(atts))
        rows = (H - 16 - 13) // self.RH
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        fb, fs = font("bold", 11), font("regular", 10)
        for r in range(rows):
            i = self.top + r
            if i >= len(atts):
                break
            a = atts[i]
            y = 16 + r * self.RH
            if i == self.sel:
                d.rectangle([0, y, W, y + self.RH - 1], fill=C["sel"])
            d.text((8, y + 2), ellipsize(fb, a.name, W - 16), font=fb, fill=C["text"])
            info = "%s  ·  %s" % (mailer.human_size(a.size), a.ctype)
            if a.idx in self.saved:
                info = "✓ saved  " + info
            d.text((8, y + 14), ellipsize(fs, info, W - 16), font=fs,
                   fill=C["ok"] if a.idx in self.saved else C["dim"])
        draw_footer(d, "Enter: save  A: save all  → " + config.download_dir(self.app.acct))


class ContactsScreen(Screen):
    """Searchable address book. Calls on_pick(contact)."""
    RH = 24
    typing = True

    def __init__(self, on_pick, query=""):
        super().__init__()
        self.on_pick = on_pick
        self.search = TextField(query, placeholder="Type to search")
        self.sel = 0
        self.top = 0
        self.results = []

    def on_show(self):
        self._update()

    def _update(self):
        self.results = self.app.contacts.search(self.search.value, limit=200)
        self.sel = min(self.sel, max(0, len(self.results) - 1))

    def on_key(self, ev):
        if ev.key == "down":
            self.sel = min(max(0, len(self.results) - 1), self.sel + 1)
        elif ev.key == "up":
            self.sel = max(0, self.sel - 1)
        elif ev.key == "enter":
            if self.results:
                c = self.results[self.sel]
                self.app.pop()
                self.on_pick(c)
        elif self.search.on_key(ev):
            self.sel = 0
            self._update()

    def draw(self, d):
        draw_bar(d, "Contacts", "%d" % len(self.app.contacts))
        self.search.draw(d, 4, 17, W - 8, 15, True, font("regular", 11))
        rows = (H - 34 - 13) // self.RH
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + rows:
            self.top = self.sel - rows + 1
        fb, fs = font("bold", 11), font("regular", 10)
        if not self.results:
            msg = "No matches" if len(self.app.contacts) else "No contacts yet (scanning Sent…)"
            d.text(((W - text_w(fs, msg)) / 2, H // 2 - 5), msg, font=fs, fill=C["dim"])
        for r in range(rows):
            i = self.top + r
            if i >= len(self.results):
                break
            c = self.results[i]
            y = 34 + r * self.RH
            if i == self.sel:
                d.rectangle([0, y, W, y + self.RH - 1], fill=C["sel"])
            d.text((8, y + 1), ellipsize(fb, c.name or c.addr, W - 16), font=fb, fill=C["text"])
            d.text((8, y + 12), ellipsize(fs, c.addr, W - 16), font=fs, fill=C["dim"])
        draw_footer(d, "Type: filter   ↑↓ choose   Enter: add   Esc: back")


class ComposeScreen(Screen):
    POP_RH = 13
    typing = True

    def __init__(self, to="", cc="", subject="", body="", reply_to=None, mode="New message"):
        super().__init__()
        self.fields = [TextField(to), TextField(cc), TextField(subject)]
        self.body = TextArea(body)
        self.reply_to = reply_to
        self.mode = mode
        self.focus = 0 if not to else 3  # 0 to, 1 cc, 2 subject, 3 body, 4 send
        self.original = (to, cc, subject, body)
        self.sending = False
        self.pop_items = []
        self.pop_sel = 0
        self.pop_closed_for = None
        self.dictating = False
        self.dict_seen = -1

    def _values(self):
        return tuple(f.value for f in self.fields) + (self.body.value,)

    # contacts ---------------------------------------------------------------
    def _token(self):
        v = self.fields[self.focus].value if self.focus in (0, 1) else ""
        return v.rsplit(",", 1)[-1].strip()

    def _update_popup(self):
        self.pop_items = []
        if self.focus not in (0, 1):
            return
        tok = self._token()
        if not tok or "<" in tok or tok == self.pop_closed_for:
            return
        hits = self.app.contacts.search(tok, limit=4)
        if len(hits) == 1 and hits[0].addr == tok.lower():
            return
        self.pop_items = hits
        self.pop_sel = min(self.pop_sel, max(0, len(hits) - 1))

    def _add_contact(self, c, field=None):
        f = self.fields[self.focus if field is None else field]
        head = f.value.rsplit(",", 1)[0] if "," in f.value else ""
        head = (head.strip() + ", ") if head.strip() else ""
        f.value = head + c.label() + ", "
        f.cur = len(f.value)
        self.pop_items = []

    # dictation --------------------------------------------------------------
    def _start_dictation(self):
        ok, why = speech.availability()
        if not ok:
            self.app.toast(why, C["warn"])
            return
        if self.focus == 4:
            self.focus = 3
        self.dictating = True
        self.app.dictation.start()

    def poll(self):
        """Called every frame; returns True when the screen must redraw."""
        if not self.dictating:
            return False
        dct = self.app.dictation
        changed = dct.version != self.dict_seen
        self.dict_seen = dct.version
        if dct.state == "done":
            target = self.body if self.focus == 3 else self.fields[self.focus]
            before = target.value[:target.cur]
            text = speech.tidy(dct.take(), before)
            if text:
                target.insert(text)
            self.dictating = False
            self._update_popup()
            return True
        if dct.state == "error":
            self.app.toast("Mic: " + dct.error, C["warn"])
            dct.state = "idle"
            self.dictating = False
            return True
        if dct.state == "idle":
            self.dictating = False
            return True
        return changed

    # keys -------------------------------------------------------------------
    def on_key(self, ev):
        if self.sending or (ev.repeat and is_mic_key(ev)):
            return
        if self.dictating:
            if is_mic_key(ev) or ev.key == "enter":
                self.app.dictation.stop()
            return
        if is_mic_key(ev):
            self._start_dictation()
            return
        if ev.ctrl and (ev.char in ("s", "\r") or ev.key == "enter"):
            self._send()
            return
        if ev.ctrl and ev.char == "k" and self.focus in (0, 1):
            self._open_picker()
            return
        if self.pop_items and ev.key in ("up", "down", "enter"):
            if ev.key == "down":
                self.pop_sel = min(len(self.pop_items) - 1, self.pop_sel + 1)
            elif ev.key == "up":
                self.pop_sel = max(0, self.pop_sel - 1)
            else:
                self._add_contact(self.pop_items[self.pop_sel])
            return
        if ev.key == "tab":
            if self.focus in (0, 1):
                self._resolve(self.focus)
            self.focus = (self.focus + 1) % 5
            self.pop_items = []
            return
        if self.focus == 4:
            if ev.key == "enter":
                self._send()
            elif ev.key in ("up", "left"):
                self.focus = 3
            elif ev.key == "down":
                self.focus = 0
            return
        if self.focus == 3:
            if ev.key == "up" and self.body.at_first_line():
                self.focus = 2
                return
            self.body.on_key(ev)
            return
        if ev.key == "enter" and self.focus in (0, 1) and not self._token():
            self._open_picker()
        elif ev.key in ("down", "enter"):
            if self.focus in (0, 1):
                self._resolve(self.focus)
            self.focus += 1
            self.pop_items = []
        elif ev.key == "up":
            if self.focus in (0, 1):
                self._resolve(self.focus)
            self.focus = 4 if self.focus == 0 else self.focus - 1
            self.pop_items = []
        else:
            self.fields[self.focus].on_key(ev)
            self.pop_closed_for = None
            self.pop_sel = 0
            self._update_popup()

    def _open_picker(self, query="", replace=None):
        field = self.focus

        def pick(c):
            if replace is None:
                self._add_contact(c, field)
            else:
                self._replace_token(field, replace, c.label())

        self.app.push(ContactsScreen(pick, query))

    def _replace_token(self, field, token, label):
        f = self.fields[field]
        toks = [label if t == token else t for t in mailer.split_recipients(f.value)]
        f.value = ", ".join(toks)
        f.cur = len(f.value)

    def _resolve(self, field):
        """Turn names in To/Cc into addresses using the contact book.

        Returns a list of (token, matches) that could not be resolved.
        """
        f = self.fields[field]
        out, problems = [], []
        for t in mailer.split_recipients(f.value):
            if mailer.is_address(t):
                out.append(t)
                continue
            hits = self.app.contacts.search(t, limit=6)
            low = t.lower()
            exact = [c for c in hits if c.name.lower() == low or c.addr.split("@")[0] == low]
            if len(exact) == 1:
                out.append(exact[0].label())
            elif len(hits) == 1:
                out.append(hits[0].label())
            else:
                out.append(t)
                problems.append((t, hits))
        new = ", ".join(out)
        if new != ", ".join(mailer.split_recipients(f.value)):
            f.value = new
            f.cur = len(new)
        return problems

    def on_esc(self):
        if self.dictating:
            self.app.dictation.cancel()
            self.dictating = False
            return
        if self.pop_items:
            self.pop_closed_for = self._token()
            self.pop_items = []
            return
        if self.sending:
            return
        if self._values() != self.original:
            self.app.push(Confirm("Discard this message?", self.app.pop))
        else:
            self.app.pop()

    def _send(self):
        for field in (0, 1):
            problems = self._resolve(field)
            if problems:
                token, hits = problems[0]
                self.focus = field
                if hits:
                    self.app.toast('More than one contact matches "%s". Pick one.' % token,
                                   C["warn"])
                    self._open_picker(token, replace=token)
                else:
                    self.app.toast('"%s" isn\'t an email address or a saved contact' % token,
                                   C["warn"])
                return
        to, cc, subject, body = self._values()
        if not mailer.recipients(to, cc):
            self.app.toast("Add a recipient", C["warn"])
            self.focus = 0
            return
        acct = self.app.acct
        m = self.reply_to
        msg = mailer.build_message(acct, to, cc, subject, body,
                                   m.message_id if m else "", m.references if m else "")
        self.sending = True

        def send():
            self.app.send_fn(acct, msg)
            try:
                self.app.client().append_sent(bytes(msg))
            except Exception:
                pass  # sent; saving the copy is best effort

        def done(_):
            self.sending = False
            self.app.remember_sent(to, cc)
            self.app.pop()
            self.app.toast("Message sent", C["ok"])

        def fail(e):
            self.sending = False
            self.app.toast(str(e), C["warn"])

        self.app.job("Sending…", send, done, fail)

    def draw(self, d):
        draw_bar(d, self.mode)
        fb = font("bold", 10)
        lbl = "Send"
        bw = text_w(fb, lbl) + 12
        bx = W - 18 - bw
        d.rectangle([bx, 2, bx + bw, 13],
                    fill=C["ok"] if self.focus == 4 else C["panel"])
        d.text((bx + 6, 2), lbl, font=fb, fill=C["bg"] if self.focus == 4 else C["text"])
        fl, ff = font("regular", 10), font("regular", 11)
        for i, lbl in enumerate(("To", "Cc", "Subject")):
            y = 17 + i * 15
            d.text((4, y + 1), lbl, font=fl, fill=C["accent"] if self.focus == i else C["dim"])
            self.fields[i].draw(d, 48, y, W - 52, 14, self.focus == i, ff)
        d.line([0, 62, W, 62], fill=C["panel"])
        self.body.draw(d, 6, 64, H - 13 - 64, self.focus == 3)
        if self.pop_items:
            y = 17 + self.focus * 15 + 15
            x0, x1 = 48, W - 4
            d.rectangle([x0, y, x1, y + self.POP_RH * len(self.pop_items) + 2],
                        fill=C["panel"], outline=C["accent"])
            fs = font("regular", 10)
            for i, c in enumerate(self.pop_items):
                yy = y + 1 + i * self.POP_RH
                if i == self.pop_sel:
                    d.rectangle([x0 + 1, yy, x1 - 1, yy + self.POP_RH - 1], fill=C["sel"])
                d.text((x0 + 4, yy), ellipsize(fs, c.label(), x1 - x0 - 8), font=fs, fill=C["text"])
        if self.dictating:
            self._draw_dictation(d)
        if self.sending:
            hint = "Sending…"
        elif self.dictating:
            hint = "Speak now.  Enter / Ctrl+Space: insert   Esc: cancel"
        elif self.pop_items:
            hint = "↑↓ pick contact   Enter: add   Esc: close list"
        elif self.focus in (0, 1):
            hint = "Type a name for suggestions. Enter on empty: contact list"
        elif self.focus == 4:
            hint = "Enter: send now   Tab: back to To"
        else:
            hint = "Tab: next  Ctrl+S: send  Ctrl+Space: dictate  Esc: cancel"
        draw_footer(d, hint)

    def _draw_dictation(self, d):
        dct = self.app.dictation
        x0, y0, x1, y1 = 10, 70, W - 10, H - 18
        d.rectangle([x0, y0, x1, y1], fill=C["panel"], outline=C["warn"])
        fb, fr = font("bold", 11), font("regular", 11)
        label = {"loading": "Loading speech model…", "listening": "Listening",
                 "finishing": "Finishing…"}.get(dct.state, "")
        d.ellipse([x0 + 8, y0 + 7, x0 + 16, y0 + 15], fill=C["warn"])
        d.text((x0 + 22, y0 + 4), label, font=fb, fill=C["text"])
        lvl = int(dct.level * 80)
        d.rectangle([x1 - 90, y0 + 8, x1 - 10, y0 + 13], fill=C["field"])
        if lvl:
            d.rectangle([x1 - 90, y0 + 8, x1 - 90 + lvl, y0 + 13], fill=C["ok"])
        preview = speech.tidy(dct.partial) if dct.partial else "…"
        lines = wrap_lines(preview, fr, x1 - x0 - 16)[-4:]
        for i, line in enumerate(lines):
            d.text((x0 + 8, y0 + 20 + i * 13), line, font=fr, fill=C["text"])


HELP_ALWAYS = [
    ("Esc", "back / close"),
    ("hold Esc", "quit the app"),
    ("Fn+H", "this help (H or ? too, when not typing)"),
    ("Fn + F X Z C", "arrow keys"),
]

HELP = {
    "InboxScreen": [
        ("up / down", "move (j / k work too)"),
        ("Enter", "open the email"),
        ("/  or Ctrl+F", "search"),
        ("C", "write a new email"),
        ("R", "check for new mail"),
        ("F", "switch folder"),
        ("D", "delete"),
        ("U", "mark read / unread"),
        ("S", "account and settings"),
    ],
    "ReaderScreen": [
        ("up / down", "scroll, Space for a page"),
        ("R / A / F", "reply, reply all, forward"),
        ("V", "switch text only / full view"),
        ("I", "pictures in the email (save them)"),
        ("L", "links (open, QR code, download)"),
        ("T", "attachments"),
        ("D", "delete"),
        ("U", "mark unread and go back"),
    ],
    "ComposeScreen": [
        ("Tab", "next field: To, Cc, Subject, text, Send"),
        ("Ctrl+S", "send (Ctrl+Enter too)"),
        ("Ctrl+Space", "dictate (Fn+Space too)"),
        ("Enter in To/Cc", "pick a suggestion, or open contacts if empty"),
        ("Ctrl+K", "contacts"),
    ],
    "SetupScreen": [
        ("up / down", "move between fields"),
        ("left / right", "change an option"),
        ("Ctrl+P", "show the password"),
        ("Enter on Save", "test the connection and save"),
    ],
    "LinksScreen": [
        ("Enter", "open in the text web view"),
        ("Q", "QR code, to open it on your phone"),
        ("S", "download what the link points to"),
    ],
    "WebScreen": [
        ("up / down", "scroll"),
        ("L", "links on this page"),
        ("I", "pictures on this page"),
        ("S", "save this page or file"),
        ("Q", "QR code for your phone"),
    ],
    "ImagesScreen": [("Enter", "save this picture"), ("A", "save all")],
    "AttachmentsScreen": [("Enter", "save this file"), ("A", "save all")],
    "ContactsScreen": [("type", "filter"), ("Enter", "add to the email")],
    "FolderScreen": [("Enter", "open the folder")],
}


class HelpScreen(DocView):
    def __init__(self, under):
        super().__init__()
        self.under = under

    def on_show(self):
        if self.items:
            return
        fb, fr, fs = font("bold", 11), font("regular", 11), font("regular", 10)
        out = []

        def keys(rows):
            for k, what in rows:
                for i, line in enumerate(wrap_lines(what, fs, W - 100)):
                    out.append(("key", (k if i == 0 else "", line), self.LH))

        here = HELP.get(type(self.under).__name__)
        if here:
            self.add_text(out, "On this screen", fb, C["accent"])
            keys(here)
            out.append(("line", ("", fs, C["dim"]), 6))
        self.add_text(out, "Anywhere", fb, C["accent"])
        keys(HELP_ALWAYS)
        out.append(("line", ("", fs, C["dim"]), 6))

        a = self.app.acct
        ok, why = speech.availability()
        try:
            import qrcode  # noqa: F401
            qr = "ready"
        except ImportError:
            qr = "needs python3-qrcode"
        self.add_text(out, "Info", fb, C["accent"])
        info = [
            ("Version", __version__),
            ("Account", a.get("email") or "not set up"),
            ("Server", a.get("imap_host") or "-"),
            ("View", "full" if a.get("view_mode") == "full" else "text only"),
            ("Web pictures", a.get("remote_images") or "off"),
            ("Downloads", config.download_dir(a)),
            ("Screen", "EXT display %dx%d" % (W, H) if H > 170 else "built-in"),
            ("Contacts", "%d known" % len(self.app.contacts)),
            ("Dictation", "ready" if ok else why),
            ("QR codes", qr),
            ("Settings", config.config_path()),
            ("Log", mailer.LOG_PATH),
        ]
        keys(info)
        self._set_items(out)

    def on_key(self, ev):
        if ev.key in ("help", "enter") or ev.char in ("?", "h", "H"):
            self.app.pop()
        else:
            self.scroll_key(ev)

    def draw(self, d):
        draw_bar(d, "Help")
        y0, vh = 16, self.view_h()
        fk, fs = font("bold", 10), font("regular", 10)
        y = -self.top
        for kind, payload, h in self.items:
            if 0 <= y < vh - 2:
                if kind == "key":
                    k, what = payload
                    d.text((6, y0 + 1 + y), k, font=fk, fill=C["unread"])
                    d.text((96, y0 + 1 + y), what, font=fs, fill=C["text"])
                elif kind == "line":
                    text, f, color = payload
                    if text:
                        d.text((5, y0 + 1 + y), text, font=f, fill=color)
            y += h
        if self.total > vh:
            th = max(6, int(vh * vh / self.total))
            ty = y0 + int((vh - th) * self.top / max(1, self.maxtop()))
            d.rectangle([W - 2, ty, W - 1, ty + th], fill=C["accent"])
        draw_footer(d, "up / down scroll   Esc close")


# ----------------------------------------------------------------- app ---

class App:
    def __init__(self, acct=None, make_client=None, send_fn=None):
        self.running = True
        self.acct = acct if acct is not None else config.load()
        self.make_client = make_client or mailer.ImapClient
        self.send_fn = send_fn or mailer.smtp_send
        self.save_fn = config.save
        self._client = None
        self.worker = Worker()
        self.pending = 0
        self.busy_label = ""
        self.stack = []
        self.toast_msg = None
        self.esc_at = None
        self.dirty = True
        self.spin = 0
        self.spin_t = 0.0
        self.contacts = ContactBook.load(self.acct.get("email", ""))
        self.contacts_synced = False
        self.dictation = speech.Dictation()
        if config.is_complete(self.acct):
            self.push(InboxScreen())
        else:
            self.push(SetupScreen(self.acct, first_run=True))

    # screen stack -------------------------------------------------------
    @property
    def top(self):
        return self.stack[-1]

    def push(self, s):
        s.app = self
        self.stack.append(s)
        s.on_show()
        self.dirty = True

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self.top.on_show()
        self.dirty = True

    def replace(self, s):
        self.stack = []
        self.push(s)

    def find(self, cls):
        for s in reversed(self.stack):
            if isinstance(s, cls):
                return s
        return None

    # services -----------------------------------------------------------
    def client(self):
        """IMAP client; call only from inside a job (worker thread)."""
        if self._client is None:
            self._client = self.make_client(self.acct)
        return self._client

    def set_account(self, acct, reconnect=True):
        changed_user = acct.get("email", "").lower() != self.acct.get("email", "").lower()
        self.acct = acct
        if reconnect:
            old = self._client
            self._client = None
            if old is not None:
                self.worker.submit(old.close, lambda _: None, lambda _: None)
        if changed_user:
            self.contacts = ContactBook.load(acct.get("email", ""))
            self.contacts_synced = False
        self.save_fn(acct)

    def save_contacts(self):
        try:
            self.contacts.save()
        except OSError:
            pass

    def sync_contacts(self):
        """Once per run: learn recipients from the Sent folder (incremental)."""
        if self.contacts_synced:
            self.save_contacts()
            return
        self.contacts_synced = True
        book = self.contacts
        last, hint = book.sent_last_uid, book.sent_folder

        def done(r):
            folder, newest, rows = r
            if book is not self.contacts:
                return
            if folder != book.sent_folder:
                book.sent_folder = folder
                book.dirty = True
            if newest != book.sent_last_uid:
                book.sent_last_uid = newest
                book.dirty = True
            for name, addr, when in rows:
                book.add(name, addr, "sent", when)
            self.save_contacts()

        def fail(_e):
            self.save_contacts()  # contact sync is best effort; stay quiet

        self.job("Contacts…", lambda: self.client().scan_sent(last, hint), done, fail)

    def download_link(self, url):
        dest = config.download_dir(self.acct)

        def done(path):
            self.toast("Saved: " + path, C["ok"])

        def fail(e):
            mailer.log("download failed (%s): %s" % (e, url[:200]))
            self.toast("Download failed: %s" % (getattr(e, "reason", None) or e), C["warn"])

        self.job("Downloading…", lambda: mailer.download_url(url, dest), done, fail)

    def remember_sent(self, to, cc):
        for name, addr in mailer.getaddresses([to, cc]):
            self.contacts.add(name, addr, "sent")
        self.save_contacts()

    def job(self, label, fn, done, fail=None):
        self.pending += 1
        self.busy_label = label
        self.dirty = True
        self.worker.submit(fn, done, fail or (lambda e: self.toast(str(e), C["warn"])))

    def toast(self, text, color=None):
        self.toast_msg = (text, color or C["text"], time.time() + TOAST_SECS)
        self.dirty = True

    def toggle_seen(self, folder, s):
        new = not s.seen

        def done(_):
            s.seen = new

        self.job("Updating…", lambda: self.client().set_seen(folder, s.uid, new), done)

    def delete(self, inbox, s):
        folder = inbox.folder

        def done(_):
            inbox.remove(s.uid)
            self.toast("Deleted", C["ok"])

        self.job("Deleting…", lambda: self.client().delete(folder, s.uid), done)

    # input / frame ------------------------------------------------------
    def handle_key(self, ev):
        if ev.key == "esc":
            if ev.down:
                if not ev.repeat and self.esc_at is None:
                    self.esc_at = time.time()
            else:
                t, self.esc_at = self.esc_at, None
                if t is not None and time.time() - t < LONG_ESC:
                    self.top.on_esc()
                    self.dirty = True
            return
        wants_help = ev.key == "help" or (
            ev.char in ("?", "h", "H") and not ev.ctrl and not getattr(self.top, "typing", False))
        if ev.down and wants_help and not isinstance(self.top, HelpScreen):
            if not ev.repeat:
                self.push(HelpScreen(self.top))
            return
        if ev.down:
            self.top.on_key(ev)
            self.dirty = True

    def tick(self, force=False):
        for cb, result, err in self.worker.drain():
            self.pending = max(0, self.pending - 1)
            try:
                cb(err if err is not None else result)
            except Exception as e:
                self.toast("Error: %s" % e, C["warn"])
            self.dirty = True
        now = time.time()
        if self.esc_at is not None and now - self.esc_at >= LONG_ESC:
            self.running = False
            return None
        if self.pending and now - self.spin_t > 0.12:
            self.spin = (self.spin + 1) % 8
            self.spin_t = now
            self.dirty = True
        poll = getattr(self.top, "poll", None)
        if poll is not None and poll():
            self.dirty = True
        if self.toast_msg and now > self.toast_msg[2]:
            self.toast_msg = None
            self.dirty = True
        if not (self.dirty or force):
            return None
        self.dirty = False
        return self.render()

    def render(self):
        img = Image.new("RGB", (W, H), C["bg"])
        d = ImageDraw.Draw(img)
        # Draw the screen under an overlay first.
        if self.top.overlay and len(self.stack) > 1:
            self.stack[-2].draw(d)
            dim = Image.new("RGB", (W, H), (0, 0, 0))
            img.paste(Image.blend(img, dim, 0.45))
            d = ImageDraw.Draw(img)
        self.top.draw(d)
        if self.pending:
            cx, cy = W - 8, 8
            import math
            for i in range(8):
                a = (i / 8.0) * 2 * math.pi
                x, y = cx + 4.5 * math.cos(a), cy + 4.5 * math.sin(a)
                on = i == self.spin
                d.ellipse([x - 1, y - 1, x + 1, y + 1],
                          fill=C["bar_text"] if on else (120, 150, 200))
        if self.toast_msg:
            text, color, _ = self.toast_msg
            f = font("regular", 10)
            lines = wrap_lines(text, f, W - 40)[:3]
            bh = 6 + 12 * len(lines)
            y0 = H - 16 - bh
            d.rectangle([12, y0, W - 12, y0 + bh], fill=C["panel"], outline=color)
            for i, line in enumerate(lines):
                d.text((20, y0 + 3 + i * 12), line, font=f, fill=color)
        return img
