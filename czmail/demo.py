"""Offline demo mailbox (no network) for trying the UI and making screenshots."""

import datetime as _dt
import os
import time

from . import mailer

DEMO_ACCOUNT = {
    "name": "Alex Demo", "email": "alex@example.com", "username": "",
    "password": "demo", "imap_host": "imap.example.com", "imap_port": "993",
    "smtp_host": "smtp.example.com", "smtp_port": "465",
}

_now = _dt.datetime.now().astimezone()

_MSGS = [
    ("Jamie Rivera <jamie@example.org>", "Trail ride Saturday?", 0.4, False,
     "Hey Alex,\n\nA few of us are heading up the ridge trail Saturday morning, "
     "meeting at the north lot around 8. Bring water, it'll be warm.\n\n"
     "Want me to save you a spot in the car?\n\nJamie"),
    ("GitHub <noreply@github.com>", "[cardputer-mail] CI passed on main", 2.5, False,
     "All checks have passed.\n\nbuild-deb  ✓ 1m 12s\nlint       ✓ 18s\n\n"
     "View the run on GitHub."),
    ("Mom <mom@example.net>", "Recipe you asked for", 26, True,
     "Here it is!\n\n2 cups flour\n1 tsp baking soda\n1/2 cup butter\n"
     "3/4 cup brown sugar\n2 eggs\n\nBake at 350 for 11 minutes. Love you."),
    ("Hardware Store <orders@example.com>", "Your order has shipped", 50, True,
     "Good news - order #48213 is on its way.\n\n1x Soldering tip set\n"
     "2x JST-PH 2-pin leads\n\nExpected delivery: Thursday."),
    ("Sam Okafor <sam@example.org>", "Re: LoRa range test results", 74, True,
     "Got 3.1 km line of sight with the stock antenna, 4.6 with the "
     "upgraded one. Spreadsheet attached.\n\n> How far did the second node get?\n"
     "> Curious if the hill blocked it."),
    ("Newsletter <news@example.com>", "This week in tiny computers", 120, True,
     "Pocket Linux devices are having a moment. This week: keyboards, "
     "batteries, and why 320x170 is the new 80x25."),
    ("Calendar <calendar@example.com>", "Reminder: dentist 3:30 PM", 170, True,
     "This is a reminder for your appointment tomorrow at 3:30 PM."),
    ("Priya N <priya@example.org>", "Photos from the meetup", 240, True,
     "Uploaded everything to the shared album. The group shot came out great."),
]


def _demo_image():
    """A small chart picture used as an inline image in the demo newsletter."""
    import io
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (280, 110), (245, 248, 252))
    d = ImageDraw.Draw(im)
    vals = [30, 52, 41, 70, 64, 88, 95]
    for i, v in enumerate(vals):
        x = 18 + i * 37
        d.rectangle([x, 100 - v, x + 24, 100], fill=(40, 110, 200))
    d.line([10, 100, 270, 100], fill=(90, 90, 90), width=2)
    d.text((12, 6), "Pocket Linux sales", fill=(30, 30, 30))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _build(uid, frm, subj, hours, body):
    from email.message import EmailMessage
    from email.utils import format_datetime
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = "Alex Demo <alex@example.com>"
    if "LoRa" in subj:
        msg["Cc"] = "Priya N <priya@example.org>"
    msg["Subject"] = subj
    msg["Date"] = format_datetime(_now - _dt.timedelta(hours=hours))
    msg["Message-ID"] = "<demo%d@example.com>" % uid
    msg.set_content(body)
    if "tiny computers" in subj:
        html = ("<html><body><h1>This week in tiny computers</h1>"
                "<p>Pocket Linux devices are having a <b>moment</b>. Sales are up "
                "again this quarter:</p><img src=\"cid:chart1\" alt=\"sales chart\">"
                "<p>Read the <a href=\"https://example.com/keyboards\">keyboard round-up</a> "
                "and our <a href=\"https://example.com/batteries\">battery test</a>.</p>"
                "<img src=\"https://example.com/track.gif\" width=\"1\" height=\"1\">"
                "<img src=\"https://example.com/hero.jpg\" alt=\"hero photo\">"
                "<ul><li>320x170 is the new 80x25</li><li>Why Fn keys matter</li></ul>"
                "</body></html>")
        msg.add_alternative(html, subtype="html")
        msg.get_payload()[1].add_related(_demo_image(), "image", "png", cid="<chart1>")
    if "LoRa" in subj:
        msg.add_attachment(b"node,distance_km\nstock,3.1\nupgraded,4.6\n",
                           maintype="text", subtype="csv", filename="range-test.csv")
        msg.add_attachment(_demo_image(), maintype="image", subtype="png",
                           filename="map.png")
    return msg.as_bytes()


class DemoClient:
    def __init__(self, acct):
        self.acct = acct
        self.boxes = {"INBOX": {}, "Sent": {}, "Archive": {}, "Trash": {}}
        for i, (frm, subj, hours, seen, body) in enumerate(reversed(_MSGS)):
            uid = 100 + i
            self.boxes["INBOX"][uid] = [_build(uid, frm, subj, hours, body), seen]
        sent_to = ["Jamie Rivera <jamie@example.org>", "Sam Okafor <sam@example.org>",
                   "Mom <mom@example.net>", "Jamie Rivera <jamie@example.org>",
                   "Jordan Lee <jordan.lee@example.com>"]
        for i, to in enumerate(sent_to):
            self.boxes["Sent"][200 + i] = [_build(200 + i, "Alex Demo <alex@example.com>",
                                                  "hi", 30 + i * 20, "hi")
                                           .replace(b"To: Alex Demo <alex@example.com>",
                                                    b"To: " + to.encode()), True]

    def _delay(self):
        if not os.environ.get("CZMAIL_DEMO_FAST"):
            time.sleep(0.35)

    def connect(self):
        self._delay()

    def close(self):
        pass

    def folders(self):
        self._delay()
        return [(k, k) for k in self.boxes]

    def search_uids(self, folder):
        self._delay()
        return sorted(self.boxes.get(folder, {}))

    def summaries(self, folder, uids):
        out = []
        for u in uids:
            raw, seen = self.boxes[folder][u]
            m = mailer.parse_message(u, raw)
            name, addr = mailer.display_name(m.sender)
            out.append(mailer.Summary(u, name, addr, m.subject, m.date, seen, len(raw)))
        out.sort(key=lambda s: s.uid, reverse=True)
        return out

    def message(self, folder, uid, mark_seen=True, full=False, remote=False):
        self._delay()
        rec = self.boxes[folder][uid]
        if mark_seen:
            rec[1] = True
        return mailer.parse_message(uid, rec[0], full=full, remote=False)

    def fetch_raw(self, folder, uid):
        return self.boxes[folder][uid][0]

    def scan_sent(self, last_uid=0, folder_hint="", limit=500):
        from email.parser import BytesParser
        from email import policy
        out, newest = [], last_uid
        for u, (raw, _) in sorted(self.boxes["Sent"].items()):
            if u <= last_uid:
                continue
            h = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
            for name, addr in mailer.getaddresses([str(h["To"] or "")]):
                out.append((name, addr, None))
            newest = u
        return "Sent", newest, out

    def search(self, folder, query, all_mail=False):
        self._delay()
        q = query.lower()
        target = "INBOX" if all_mail else folder
        hits = [u for u, (raw, _) in self.boxes.get(target, {}).items()
                if q in mailer.parse_message(u, raw).text.lower()
                or q in raw.split(b"\n\n", 1)[0].decode("utf-8", "replace").lower()]
        return target, sorted(hits)

    def set_seen(self, folder, uid, seen):
        self.boxes[folder][uid][1] = seen

    def delete(self, folder, uid):
        self.boxes["Trash"][uid] = self.boxes[folder].pop(uid)

    def append_sent(self, msg_bytes):
        return True


def demo_send(acct, msg, test_only=False):
    if not os.environ.get("CZMAIL_DEMO_FAST"):
        time.sleep(0.5)


def make_demo_app():
    from .app import App
    app = App(acct=dict(DEMO_ACCOUNT), make_client=DemoClient, send_fn=demo_send)
    app.save_fn = lambda a: None  # never write the demo account to disk
    app.save_contacts = lambda: None
    return app


def screenshots(out_dir):
    """Render the four 320x170 store screenshots."""
    from .display import pump
    from .keyboard import Key

    os.environ["CZMAIL_DEMO_FAST"] = "1"
    os.makedirs(out_dir, exist_ok=True)
    app = make_demo_app()

    def key(k=None, ch=None, ctrl=False):
        app.handle_key(Key(True, key=k, char=ch, ctrl=ctrl))
        app.handle_key(Key(False, key=k, char=ch, ctrl=ctrl))

    def esc():
        app.esc_at = None
        app.handle_key(Key(True, key="esc"))
        app.handle_key(Key(False, key="esc"))

    def shot(name):
        img = pump(app, 0.3)
        app.pending = 0
        app.toast_msg = None
        img = app.tick(force=True)
        path = os.path.join(out_dir, name)
        img.save(path)
        return path

    paths = [shot("inbox.png")]
    for _ in range(5):
        key("down")
    key("enter")                      # newsletter
    pump(app, 0.1)
    key(ch="v")                       # full view
    pump(app, 0.1)
    for _ in range(4):
        key("down")
    paths.append(shot("reader-full.png"))
    esc()
    key("up")
    key("enter")                      # LoRa mail with attachments
    pump(app, 0.1)
    key(ch="t")
    paths.append(shot("attachments.png"))
    esc()
    esc()
    key(ch="c")
    for ch in "ja":
        key(ch=ch)
    paths.append(shot("compose.png"))
    esc()                             # close suggestions
    esc()
    pump(app, 0.05)
    key("enter")                      # confirm discard
    key(ch="s")
    for _ in range(8):
        key("down")
    paths.append(shot("setup.png"))
    return paths
