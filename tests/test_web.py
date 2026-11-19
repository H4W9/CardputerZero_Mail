"""Links, web view, images and search - offline, using a local HTTP server."""

import http.server
import io
import os
import shutil
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["CZMAIL_DEMO_FAST"] = "1"

from PIL import Image  # noqa: E402

from czmail import mailer  # noqa: E402
from czmail.keyboard import Key  # noqa: E402
from czmail.display import pump  # noqa: E402


def _png(color=(200, 40, 40), size=(64, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


PAGE = (b"<html><head><title>Test Page</title></head><body><h1>Hello</h1>"
        b"<p>Go <a href='/next.html'>next</a> or <a href='https://example.org/x'>out</a>.</p>"
        b"<img src='pic.png' alt='pic'></body></html>")
FILES = {
    "/page.html": ("text/html; charset=utf-8", PAGE, {}),
    "/pic.png": ("image/png", _png(), {}),
    "/report": ("application/pdf", b"%PDF-1.4 fake", {"Content-Disposition": 'attachment; filename="report.pdf"'}),
}


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        f = FILES.get(self.path)
        if not f:
            self.send_error(404)
            return
        ctype, body, extra = f
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = os.path.join(os.path.dirname(__file__), "_tmp_web")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_fetch_page_html(self):
        pg = mailer.fetch_page(self.base + "/page.html", remote=True)
        self.assertEqual(pg.kind, "html")
        self.assertEqual(pg.title, "Test Page")
        self.assertEqual(pg.links, [self.base + "/next.html", "https://example.org/x"])
        self.assertEqual(pg.labels[self.base + "/next.html"], "next")
        self.assertEqual(len(pg.images), 1)          # relative img resolved + loaded
        self.assertEqual(pg.images[0].name, "pic.png")

    def test_fetch_image_and_file(self):
        pg = mailer.fetch_page(self.base + "/pic.png")
        self.assertEqual(pg.kind, "image")
        pg = mailer.fetch_page(self.base + "/report")
        self.assertEqual(pg.kind, "file")
        self.assertEqual(pg.filename, "report.pdf")
        path = mailer.download_url(self.base + "/report", self.tmp)
        self.assertTrue(path.endswith("report.pdf"))
        path = mailer.save_bytes(_png(), self.tmp, "noext")
        self.assertTrue(path.endswith("noext.png"))

    def test_message_links_and_images(self):
        from czmail.demo import DemoClient
        c = DemoClient({})
        news = [u for u, (raw, _) in c.boxes["INBOX"].items() if b"tiny computers" in raw][0]
        m = c.message("INBOX", news)                  # text view still knows the links
        self.assertIn("https://example.com/keyboards", m.links)
        self.assertEqual(m.link_labels["https://example.com/keyboards"], "keyboard round-up")
        m = c.message("INBOX", news, full=True)
        self.assertEqual(len(m.images), 1)
        self.assertTrue(m.images[0].data.startswith(b"\x89PNG"))
        self.assertEqual(mailer.find_urls("see https://a.b/c. and http://x.y/z)"),
                         ["https://a.b/c", "http://x.y/z"])

    def test_ui_flow_search_links_images_qr(self):
        from czmail.app import (ImagesScreen, InboxScreen, LinksScreen, QRScreen,
                                ReaderScreen, SearchPrompt, WebScreen)
        from czmail.demo import make_demo_app
        app = make_demo_app()
        app.acct["download_dir"] = self.tmp

        def key(k=None, ch=None, ctrl=False):
            app.handle_key(Key(True, key=k, char=ch, ctrl=ctrl))
            app.handle_key(Key(False, key=k, char=ch, ctrl=ctrl))

        def esc():
            app.handle_key(Key(True, key="esc"))
            app.handle_key(Key(False, key="esc"))

        def settle():
            for _ in range(80):
                pump(app, 0.02)
                if not app.pending:
                    return

        settle()
        inbox = app.top
        key(ch="/")
        self.assertIsInstance(app.top, SearchPrompt)
        for ch in "computers":
            key(ch=ch)
        key("enter")
        settle()
        res = app.top
        self.assertIsInstance(res, InboxScreen)
        self.assertEqual(len(res.items), 1)
        self.assertIn("tiny computers", res.items[0].subject)
        key("enter")                                   # open the newsletter (text view)
        settle()
        rd = app.top
        self.assertIsInstance(rd, ReaderScreen)
        key(ch="l")
        self.assertIsInstance(app.top, LinksScreen)
        key(ch="q")
        self.assertIsInstance(app.top, QRScreen)
        app.tick(force=True)                            # renders with or without qrcode
        esc()
        esc()
        key(ch="i")                                    # switches to Full view, then lists images
        settle()
        self.assertIsInstance(app.top, ImagesScreen)
        key("enter")
        settle()
        self.assertTrue(any(f.startswith("chart1") for f in os.listdir(self.tmp)))
        esc()
        self.assertIs(app.top, rd)
        # web view on a local page
        app.push(WebScreen(self.base + "/page.html"))
        settle()
        web = app.top
        self.assertEqual(web.page.title, "Test Page")
        app.tick(force=True)
        esc()
        esc()
        self.assertIs(app.top, res)
        esc()                                          # leave search results
        self.assertIs(app.top, inbox)


if __name__ == "__main__":
    unittest.main()
