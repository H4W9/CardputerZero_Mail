"""EXT display (DisplayMOD 2.8in screen) detection and mirror hand-off."""

import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from czmail import extdisplay  # noqa: E402

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_ext")


class ExtTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def proc(self, text):
        p = os.path.join(TMP, "fb")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_find_ext_fb(self):
        p = self.proc("0 fb_st7789v\n1 fb_ili9341\n")
        with mock.patch("os.access", return_value=True):
            self.assertEqual(extdisplay.find_ext_fb(p), "/dev/fb1")
        self.assertIsNone(extdisplay.find_ext_fb(self.proc("0 fb_st7789v\n")))
        self.assertIsNone(extdisplay.find_ext_fb(os.path.join(TMP, "missing")))

    def test_choose_respects_setting(self):
        with mock.patch.object(extdisplay, "find_ext_fb", return_value="/dev/fb1"):
            self.assertEqual(extdisplay.choose("auto"), "/dev/fb1")
            self.assertIsNone(extdisplay.choose("builtin"))
            with mock.patch.dict(os.environ, {"CZMAIL_SCREEN": "builtin"}):
                self.assertIsNone(extdisplay.choose("auto"))

    def test_store_edition_uses_displaymod_send(self):
        calls = []
        with mock.patch("os.access", return_value=True), \
                mock.patch("subprocess.run", side_effect=lambda a, **k: calls.append(a)):
            extdisplay.claim()
            extdisplay.release()
        self.assertEqual(calls[0][:3], [extdisplay.SEND, "ui", "on"])
        self.assertEqual(calls[0][3], str(os.getpid()))
        self.assertEqual(calls[1], [extdisplay.SEND, "ui", "off"])

    def test_legacy_flag_file(self):
        run = os.path.join(TMP, "run_user_1000")
        os.makedirs(run)
        open(os.path.join(run, "displaymod-ui-ack"), "w").close()   # mirror answers at once
        with mock.patch("os.access", return_value=False), \
                mock.patch.object(extdisplay.os, "getuid", create=True, return_value=1000), \
                mock.patch("glob.glob", return_value=[run]), \
                mock.patch.object(extdisplay.os.path, "join",
                                  side_effect=lambda *a: "/".join(a) if a[0] != "/run/user/1000"
                                  else run + "/" + a[1]):
            extdisplay.claim()
            flag = os.path.join(run, "displaymod-ui")
            with open(flag) as fh:
                self.assertEqual(fh.read().strip(), str(os.getpid()))
            extdisplay.release()
            self.assertFalse(os.path.exists(flag))

    def test_layout_at_ext_size_and_builtin_copy(self):
        from PIL import Image
        from czmail.display import builtin_copy
        im = builtin_copy(Image.new("RGB", (320, 240), (255, 0, 0)))
        self.assertEqual(im.size, (320, 170))
        self.assertEqual(im.getpixel((160, 85)), (255, 0, 0))
        self.assertEqual(im.getpixel((5, 85)), (0, 0, 0))            # black side bars


if __name__ == "__main__":
    unittest.main()
