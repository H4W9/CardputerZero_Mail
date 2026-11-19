"""Local address book learned from your mail.

Sources: recipients in your Sent folder (strongest signal), people you send
to from this app, and senders of mail you receive. Stored next to the
account file as contacts.json. Main (UI) thread only.
"""

import json
import os
import re
import time
from email.utils import formataddr

from . import config

_ROBOT = re.compile(r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce|"
                    r"notifications?@|notify@|newsletter|unsubscribe)", re.I)


def contacts_path():
    return os.path.join(os.path.dirname(config.config_path()), "contacts.json")


class Contact:
    __slots__ = ("addr", "name", "sent", "recv", "last")

    def __init__(self, addr, name="", sent=0, recv=0, last=0.0):
        self.addr, self.name, self.sent, self.recv, self.last = addr, name, sent, recv, last

    def label(self):
        return formataddr((self.name, self.addr)) if self.name else self.addr

    def score(self, now):
        age_days = max(0.0, (now - self.last) / 86400.0)
        return self.sent * 5 + self.recv + 4.0 / (1.0 + age_days / 30.0)


class ContactBook:
    def __init__(self, account=""):
        self.account = account.lower()
        self.items = {}
        self.sent_folder = ""
        self.sent_last_uid = 0
        self.dirty = False

    # persistence ---------------------------------------------------------
    @classmethod
    def load(cls, account):
        book = cls(account)
        try:
            with open(contacts_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return book
        if str(data.get("account", "")).lower() != book.account:
            return book  # different account: start fresh
        book.sent_folder = data.get("sent_folder", "")
        book.sent_last_uid = int(data.get("sent_last_uid", 0) or 0)
        for c in data.get("contacts", []):
            try:
                a = str(c["addr"]).lower()
                book.items[a] = Contact(a, c.get("name", ""), int(c.get("sent", 0)),
                                        int(c.get("recv", 0)), float(c.get("last", 0)))
            except (KeyError, TypeError, ValueError):
                continue
        return book

    def save(self):
        if not self.dirty:
            return
        path = contacts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "account": self.account,
            "sent_folder": self.sent_folder,
            "sent_last_uid": self.sent_last_uid,
            "contacts": [{"addr": c.addr, "name": c.name, "sent": c.sent,
                          "recv": c.recv, "last": c.last} for c in self.items.values()],
        }
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        self.dirty = False

    # learning ------------------------------------------------------------
    def add(self, name, addr, kind, when=None):
        addr = (addr or "").strip().lower()
        if "@" not in addr or addr == self.account or len(addr) > 254:
            return
        name = (name or "").strip().strip('"').strip()
        if name.lower() == addr:
            name = ""
        c = self.items.get(addr)
        if c is None:
            c = self.items[addr] = Contact(addr)
        if name and (not c.name or kind == "sent"):
            c.name = name
        if kind == "sent":
            c.sent += 1
        else:
            c.recv += 1
        ts = when.timestamp() if hasattr(when, "timestamp") else (when or time.time())
        c.last = max(c.last, ts)
        self.dirty = True

    def learn_received(self, summaries):
        for s in summaries:
            if s.sender_addr and not _ROBOT.search(s.sender_addr):
                key = s.sender_addr.lower()
                c = self.items.get(key)
                # count each message once per session: only if newer than last seen
                ts = s.date.timestamp() if s.date else 0
                if c is None or ts > c.last:
                    self.add(s.sender if s.sender != s.sender_addr else "",
                             s.sender_addr, "recv", s.date)

    # searching -----------------------------------------------------------
    def search(self, query, limit=6):
        q = query.strip().lower()
        now = time.time()
        out = []
        for c in self.items.values():
            if c.sent == 0 and _ROBOT.search(c.addr):
                continue
            if q:
                name = c.name.lower()
                words = re.split(r"[\s.,'\-_]+", name) + [c.addr]
                if any(w.startswith(q) for w in words if w):
                    rank = 0
                elif q in name or q in c.addr:
                    rank = 1
                else:
                    continue
            else:
                rank = 0
            out.append((rank, -c.score(now), c.addr, c))
        out.sort(key=lambda t: t[:3])
        return [t[3] for t in out[:limit]]

    def __len__(self):
        return len(self.items)
