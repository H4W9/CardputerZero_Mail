"""Offline tests: python -m unittest discover -s tests"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["CZMAIL_DEMO_FAST"] = "1"

from PIL import Image  # noqa: E402

from czmail import config, mailer  # noqa: E402
from czmail.display import pump, to_rgb565  # noqa: E402
from czmail.keyboard import EvdevKeyboard, Key, load_custom_keymap  # noqa: E402
from czmail.ui import font, wrap_offsets  # noqa: E402

RAW = (b"From: =?utf-8?q?J=C3=BCrgen?= <j@example.de>\r\n"
       b"To: me@example.com, Bob <bob@example.com>\r\n"
       b"Cc: carol@example.com\r\n"
       b"Subject: Hallo\r\nDate: Tue, 17 Nov 2026 10:00:00 +0100\r\n"
       b"Message-ID: <abc@example.de>\r\nMIME-Version: 1.0\r\n"
       b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
       b"--XX\r\nContent-Type: multipart/alternative; boundary=YY\r\n\r\n"
       b"--YY\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
       b"<html><head><style>p{}</style></head><body><p>Hi&nbsp;there</p><ul><li>one</li></ul></body></html>\r\n"
       b"--YY--\r\n"
       b"--XX\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"plan.pdf\"\r\n"
       b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n--XX--\r\n")


class MailerTests(unittest.TestCase):
    def test_parse_message_html_and_attachment(self):
        m = mailer.parse_message(5, RAW)
        self.assertIn("Jürgen", m.sender)
        self.assertEqual(m.subject, "Hallo")
        self.assertIn("Hi there", m.text.replace(" ", " "))
        self.assertIn("• one", m.text)
        self.assertNotIn("p{}", m.text)
        self.assertEqual([a.name for a in m.attachments], ["plan.pdf"])

    def test_parse_summaries_flags_after_literal(self):
        data = [
            (b'1 (UID 42 RFC822.SIZE 900 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {60}',
             b"From: Ann <ann@x.org>\r\nSubject: Yo\r\nDate: Tue, 17 Nov 2026 10:00:00 +0000\r\n\r\n"),
            b" FLAGS (\\Seen))",
            (b'2 (UID 43 FLAGS () BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {30}',
             b"From: bob@x.org\r\nSubject: =?utf-8?b?w6Rw?=\r\n\r\n"),
            b")",
        ]
        s = mailer.parse_summaries(data)
        self.assertEqual([x.uid for x in s], [43, 42])
        self.assertFalse(s[0].seen)
        self.assertTrue(s[1].seen)
        self.assertEqual(s[1].sender, "Ann")
        self.assertEqual(s[0].subject, "äp")
        self.assertEqual(s[1].size, 900)

    def test_list_and_utf7(self):
        self.assertEqual(mailer.parse_list_line(b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'),
                         ("[Gmail]/Sent Mail", ["\\HasNoChildren", "\\Sent"]))
        self.assertEqual(mailer.parse_list_line(b'(\\HasNoChildren) "." INBOX')[0], "INBOX")
        self.assertEqual(mailer.imap_utf7_decode("Entw&APw-rfe"), "Entwürfe")
        self.assertEqual(mailer.imap_utf7_decode("A&-B"), "A&B")

    def test_reply_all_excludes_self(self):
        m = mailer.parse_message(5, RAW)
        to, cc, subj, body = mailer.reply_fields({"email": "me@example.com"}, m, reply_all=True)
        self.assertIn("j@example.de", to)
        self.assertIn("bob@example.com", cc)
        self.assertIn("carol@example.com", cc)
        self.assertNotIn("me@example.com", cc)
        self.assertEqual(subj, "Re: Hallo")
        self.assertIn("> ", body)
        self.assertEqual(mailer.forward_fields(m)[2], "Fwd: Hallo")

    def test_build_message(self):
        a = dict(config.empty_account(), email="me@example.com", name="Me")
        msg = mailer.build_message(a, "x@y.com", "", "S", "body", "<p@q>", "<o@q>")
        self.assertEqual(msg["In-Reply-To"], "<p@q>")
        self.assertEqual(msg["References"], "<o@q> <p@q>")
        self.assertIn("Me <me@example.com>", msg["From"])
        self.assertEqual(mailer.recipients("A <a@b.c>, bad", "c@d.e"), ["a@b.c", "c@d.e"])


class FeatureTests(unittest.TestCase):
    def test_full_blocks_links_images(self):
        html = ('<h1>Title</h1><p>See <a href="https://x.org/a">this</a></p>'
                '<img src="https://t.example/p.gif" width="1" height="1">'
                '<img src="https://x.org/big.jpg" alt="Big">'
                '<blockquote>quoted</blockquote>')
        b = mailer.html_to_blocks(html)
        self.assertEqual(b[0], ("text", "Title", "bold"))
        self.assertIn(("text", "See this [1]", "normal"), b)
        self.assertIn(("img", "https://x.org/big.jpg", "Big"), b)
        self.assertNotIn("p.gif", str(b))          # tracking pixel dropped
        self.assertIn(("text", "quoted", "dim"), b)
        self.assertEqual(b[-1], ("text", "[1] https://x.org/a", "link"))

    def test_full_mode_inline_cid_image_and_save(self):
        from czmail.demo import DemoClient
        c = DemoClient({})
        news = [u for u, (raw, _) in c.boxes["INBOX"].items() if b"tiny computers" in raw][0]
        m = c.message("INBOX", news, full=True)
        kinds = [blk[0] for blk in m.blocks]
        self.assertIn("img", kinds)
        self.assertTrue(any("remote image" in blk[1] for blk in m.blocks if blk[0] == "text"))
        lora = [u for u, (raw, _) in c.boxes["INBOX"].items() if b"LoRa" in raw][0]
        m = c.message("INBOX", lora)
        self.assertEqual([a.name for a in m.attachments], ["range-test.csv", "map.png"])
        d = os.path.join(os.path.dirname(__file__), "_tmp_dl")
        try:
            p1 = mailer.save_attachment(m.raw, m.attachments[0].idx, d, "range-test.csv")
            p2 = mailer.save_attachment(m.raw, m.attachments[0].idx, d, "range-test.csv")
            with open(p1, "rb") as fh:
                self.assertTrue(fh.read().startswith(b"node,distance_km"))
            self.assertTrue(p2.endswith("range-test (1).csv"))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(mailer.safe_filename("../../etc/pass:wd"), "pass_wd")

    def test_contact_book_ranking(self):
        from czmail.contacts import ContactBook
        b = ContactBook("me@x.org")
        b.add("Jamie Rivera", "jamie@x.org", "sent")
        b.add("", "jam@spam.org", "recv")
        b.add("", "noreply@shop.com", "recv")
        b.add("Me", "me@x.org", "sent")
        self.assertEqual([c.addr for c in b.search("jam")], ["jamie@x.org", "jam@spam.org"])
        self.assertEqual([c.addr for c in b.search("riv")], ["jamie@x.org"])
        self.assertEqual(b.search("noreply"), [])
        self.assertEqual(b.search("me@"), [])
        self.assertEqual(b.search("jamie")[0].label(), "Jamie Rivera <jamie@x.org>")

    def test_speech_tidy(self):
        from czmail.speech import tidy
        self.assertEqual(tidy("hello there comma how are you question mark"),
                         "Hello there, how are you?")
        self.assertEqual(tidy("see you period i will call", "Hi."), " See you. I will call")
        self.assertEqual(tidy("new line thanks", "Bye"), "\nThanks")


class ConfigTests(unittest.TestCase):
    def test_preset_and_roundtrip(self):
        a = dict(config.empty_account(), email="me@GMAIL.com", imap_host="")
        self.assertTrue(config.apply_preset(a))
        self.assertEqual(a["smtp_host"], "smtp.gmail.com")
        path = os.path.join(os.path.dirname(__file__), "_tmp_config.json")
        os.environ["CZMAIL_CONFIG"] = path
        try:
            a["password"] = "pw"
            config.save(a)
            self.assertEqual(config.load()["password"], "pw")
        finally:
            del os.environ["CZMAIL_CONFIG"]
            os.remove(path)


class UiTests(unittest.TestCase):
    def test_wrap_offsets_cover_text(self):
        t = "word " * 40 + "\n\nsupercalifragilisticexpialidocious" * 3
        f = font("regular", 11)
        lines = wrap_offsets(t, f, 120)
        for a, b in lines:
            self.assertLessEqual(f.getlength(t[a:b]), 120)
        self.assertEqual(lines[-1][1], len(t))

    def test_rgb565(self):
        img = Image.new("RGB", (2, 1))
        img.putpixel((0, 0), (255, 0, 0))
        img.putpixel((1, 0), (0, 0, 255))
        self.assertEqual(to_rgb565(img), b"\x00\xf8\x1f\x00")

    def test_keyboard_translation(self):
        kb = EvdevKeyboard.__new__(EvdevKeyboard)
        kb.custom, kb.pressed = {}, {}
        kb.shift = kb.ctrl = kb.fn = kb.caps = False
        self.assertEqual(kb._translate(30, 1).char, "a")
        kb._translate(42, 1)
        self.assertEqual(kb._translate(30, 1).char, "A")
        kb._translate(42, 0)
        kb._translate(0x1D0, 1)            # Fn down
        self.assertEqual(kb._translate(35, 1).key, "help")
        kb._translate(0x1D0, 0)            # Fn released first...
        self.assertEqual(kb._translate(35, 0).key, "help")  # ...release still matches
        self.assertEqual(kb._translate(138, 1).key, "help")  # CPZ driver: Fn+H = KEY_HELP
        self.assertEqual(kb._translate(103, 1).key, "up")

    def test_keymap_file(self):
        p = os.path.join(os.path.dirname(__file__), "_tmp.map")
        with open(p, "w") as fh:
            fh.write("# test\nkeycode 26 = exclam\nkeycode 200 = Up\nbad line\n")
        try:
            m = load_custom_keymap(p)
        finally:
            os.remove(p)
        self.assertEqual(m, {26: ("char", "!"), 200: ("key", "up")})


class AppFlowTests(unittest.TestCase):
    def test_demo_flow(self):
        from czmail.app import ComposeScreen, InboxScreen, ReaderScreen, FolderScreen
        from czmail.demo import make_demo_app
        sent = []
        app = make_demo_app()
        app.send_fn = lambda a, m, test_only=False: sent.append(m)

        def key(k=None, ch=None, ctrl=False):
            app.handle_key(Key(True, key=k, char=ch, ctrl=ctrl))
            app.handle_key(Key(False, key=k, char=ch, ctrl=ctrl))

        def settle():
            for _ in range(50):
                pump(app, 0.02)
                if not app.pending:
                    return

        settle()
        inbox = app.top
        self.assertIsInstance(inbox, InboxScreen)
        self.assertEqual(len(inbox.items), 8)
        key("enter")
        settle()
        self.assertIsInstance(app.top, ReaderScreen)
        self.assertTrue(app.top.items)
        key(ch="a")
        self.assertIsInstance(app.top, ComposeScreen)
        for c in "ok":
            key(ch=c)
        key(ch="s", ctrl=True)
        settle()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["Subject"].startswith("Re:"))
        self.assertIsInstance(app.top, ReaderScreen)
        app.handle_key(Key(True, key="esc"))
        app.handle_key(Key(False, key="esc"))
        self.assertIs(app.top, inbox)
        key(ch="d")
        key("enter")
        settle()
        self.assertEqual(len(inbox.items), 7)
        key(ch="f")
        settle()
        self.assertIsInstance(app.top, FolderScreen)
        key("down")
        key("enter")
        settle()
        self.assertIs(app.top, inbox)
        self.assertNotEqual(inbox.folder, "INBOX")
        # contacts learned from Sent: autocomplete + picker
        key(ch="c")
        comp = app.top
        for ch in "jam":
            key(ch=ch)
        self.assertTrue(comp.pop_items)
        self.assertEqual(comp.pop_items[0].addr, "jamie@example.org")
        key("enter")
        self.assertEqual(comp.fields[0].value, "Jamie Rivera <jamie@example.org>, ")
        key("down")                       # to Cc, empty -> Enter opens picker
        key("enter")
        from czmail.app import ContactsScreen, SetupScreen
        self.assertIsInstance(app.top, ContactsScreen)
        for ch in "jord":
            key(ch=ch)
        key("enter")
        self.assertIs(app.top, comp)
        self.assertIn("jordan.lee@example.com", comp.fields[1].value)
        # mic key without Vosk installed -> toast, no crash
        key(ch=" ", ctrl=True)
        self.assertFalse(comp.dictating)
        app.handle_key(Key(True, key="esc"))
        app.handle_key(Key(False, key="esc"))
        key("enter")                      # discard
        self.assertIs(app.top, inbox)
        # view-mode change saves without a connection test
        key(ch="s")
        setup = app.top
        self.assertIsInstance(setup, SetupScreen)
        setup.focus = 8
        key("right")
        setup.focus = len(setup.fields)
        key("enter")
        self.assertEqual(app.acct["view_mode"], "full")
        self.assertIs(app.top, inbox)
        # long Esc exits
        app.handle_key(Key(True, key="esc"))
        app.esc_at -= 1.0
        app.tick()
        self.assertFalse(app.running)


if __name__ == "__main__":
    unittest.main()
