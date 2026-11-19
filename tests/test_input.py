"""Recipient name resolving and software key repeat."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["CZMAIL_DEMO_FAST"] = "1"

from czmail import mailer  # noqa: E402
from czmail.display import pump  # noqa: E402
from czmail.keyboard import EvdevKeyboard, Key  # noqa: E402


def bare_keyboard():
    kb = EvdevKeyboard.__new__(EvdevKeyboard)
    kb.custom, kb.pressed = {}, {}
    kb.shift = kb.ctrl = kb.fn = kb.caps = False
    kb.rep, kb.kernel_repeat = None, False
    return kb


class RepeatTests(unittest.TestCase):
    def test_held_key_repeats_after_delay(self):
        kb = bare_keyboard()
        first = kb._translate(30, 1)                      # 'a' down
        self.assertEqual(first.char, "a")
        t0 = kb.rep[2]
        self.assertEqual(kb._soft_repeat(t0 - 0.01), [])   # not yet (500 ms delay)
        reps = kb._soft_repeat(t0 + 0.12)                  # 3 repeats at 50 ms
        self.assertEqual([r.char for r in reps], ["a", "a", "a"])
        self.assertTrue(all(r.repeat and r.down for r in reps))
        kb._translate(30, 0)                               # release stops it
        self.assertEqual(kb._soft_repeat(time.monotonic() + 5), [])

    def test_only_editing_and_nav_keys_repeat(self):
        kb = bare_keyboard()
        kb._translate(28, 1)                               # Enter
        self.assertIsNone(kb.rep)
        kb._translate(28, 0)
        kb._translate(14, 1)                               # Backspace
        self.assertIsNotNone(kb.rep)
        kb._translate(14, 0)
        kb._translate(108, 1)                              # Down arrow
        self.assertIsNotNone(kb.rep)

    def test_kernel_repeat_disables_ours(self):
        kb = bare_keyboard()
        kb._translate(30, 1)
        kb._translate(30, 2)                               # kernel autorepeat seen
        self.assertEqual(kb._soft_repeat(time.monotonic() + 5), [])


class RecipientTests(unittest.TestCase):
    def test_split_and_check(self):
        self.assertEqual(mailer.split_recipients('a@b.co, "Doe, J" <j@x.org>; sam'),
                         ["a@b.co", '"Doe, J" <j@x.org>', "sam"])
        self.assertTrue(mailer.is_address("Jamie <jamie@example.org>"))
        self.assertFalse(mailer.is_address("jamie"))
        self.assertFalse(mailer.is_address("jamie@localhost"))

    def test_compose_resolves_names(self):
        from czmail.app import ComposeScreen, ContactsScreen
        from czmail.demo import make_demo_app
        sent = []
        app = make_demo_app()
        app.send_fn = lambda a, m, test_only=False: sent.append(m)
        for _ in range(60):                                # inbox + Sent scan -> contacts
            pump(app, 0.02)
            if not app.pending and len(app.contacts):
                break

        def key(k=None, ch=None, ctrl=False):
            app.handle_key(Key(True, key=k, char=ch, ctrl=ctrl))
            app.handle_key(Key(False, key=k, char=ch, ctrl=ctrl))

        app.contacts.add("Sam Two", "sam.two@example.net", "sent")  # make "sam" ambiguous
        key(ch="c")
        comp = app.top
        self.assertIsInstance(comp, ComposeScreen)
        for ch in "jordan":                                # unique name, not picked from list
            key(ch=ch)
        key(k="tab")                                        # leaving the field resolves it
        self.assertEqual(comp.fields[0].value, "Jordan Lee <jordan.lee@example.com>")

        comp.fields[1].value = "sa"                         # ambiguous -> picker on send
        comp.fields[1].cur = 2
        key(ch="s", ctrl=True)
        self.assertIsInstance(app.top, ContactsScreen)
        self.assertEqual(app.top.search.value, "sa")
        key("enter")                                        # first match
        self.assertIs(app.top, comp)
        self.assertIn("@", comp.fields[1].value)

        comp.fields[1].value = "nobody-here"                # no match -> clear message
        key(ch="s", ctrl=True)
        self.assertIs(app.top, comp)
        self.assertIn("isn't an email address", app.toast_msg[0])

        comp.fields[1].value = ""
        key(ch="s", ctrl=True)
        for _ in range(40):
            pump(app, 0.02)
            if sent:
                break
        self.assertEqual(len(sent), 1)
        self.assertIn("jordan.lee@example.com", sent[0]["To"])


if __name__ == "__main__":
    unittest.main()
