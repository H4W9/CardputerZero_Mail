"""IMAP (read) and SMTP (send), using only the Python standard library."""

import base64
import datetime as _dt
import imaplib
import re
import smtplib
import socket
import ssl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import (formataddr, formatdate, make_msgid,
                         parseaddr, parsedate_to_datetime)
from html.parser import HTMLParser
import io
import os
import urllib.request
from urllib.parse import urljoin, urlparse

LOG_PATH = os.environ.get("CZMAIL_LOG", "/tmp/czmail.log")


def log(msg):
    """Append a line to /tmp/czmail.log (best effort, for troubleshooting)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (_dt.datetime.now().strftime("%H:%M:%S"), msg))
    except OSError:
        pass


if "MOVE" not in imaplib.Commands:  # RFC 6851; older Pythons lack it
    imaplib.Commands["MOVE"] = ("SELECTED",)

FETCH_LIMIT = 512 * 1024        # bytes downloaded to read a message (text view)
FETCH_LIMIT_FULL = 6 * 1024 * 1024   # full view: room for inline images
RAW_LIMIT = 40 * 1024 * 1024    # attachment download cap
TIMEOUT = 25
IMG_W, IMG_MAXH = 306, 420
MAX_REMOTE_IMAGES = 20
REMOTE_IMAGE_BYTES = 12 * 1024 * 1024  # big photos; JPEG draft mode keeps decode memory low


class MailError(Exception):
    pass


class Summary:
    __slots__ = ("uid", "sender", "sender_addr", "subject", "date", "seen", "size")

    def __init__(self, uid, sender="", sender_addr="", subject="", date=None,
                 seen=True, size=0):
        self.uid = uid
        self.sender = sender
        self.sender_addr = sender_addr
        self.subject = subject
        self.date = date
        self.seen = seen
        self.size = size


class Message:
    def __init__(self, uid):
        self.uid = uid
        self.sender = ""
        self.to = ""
        self.cc = ""
        self.reply_to = ""
        self.subject = ""
        self.date = None
        self.date_raw = ""
        self.message_id = ""
        self.references = ""
        self.text = ""
        self.attachments = []   # [Attachment]
        self.blocks = None      # full view: [('text', str, style) | ('img', PIL.Image)]
        self.full = False
        self.truncated = False
        self.raw = None         # kept when the whole message was downloaded
        self.image_count = 0    # images a Full view would show
        self.images = []        # [ImageRef] (Full view)
        self.links = []         # [url]
        self.link_labels = {}   # url -> link text


class Attachment:
    __slots__ = ("idx", "name", "ctype", "size")

    def __init__(self, idx, name, ctype, size):
        self.idx, self.name, self.ctype, self.size = idx, name, ctype, size


def human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d" % n


# ----------------------------------------------------------- helpers -----

def _is_local(host):
    return host in ("127.0.0.1", "localhost", "::1")


def _ssl_ctx(host):
    ctx = ssl.create_default_context()
    if _is_local(host):  # e.g. Proton Mail Bridge, self-signed
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _hdr(msg, name):
    try:
        v = msg.get(name)
        return str(v).strip() if v is not None else ""
    except Exception:
        try:
            return str(msg.get_all(name, [""])[0])
        except Exception:
            return ""


def _date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.astimezone()
    except (TypeError, ValueError, IndexError):
        return None


def short_date(d, now=None):
    if d is None:
        return ""
    now = now or _dt.datetime.now().astimezone()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.year == now.year:
        return d.strftime("%b %d").replace(" 0", " ")
    return d.strftime("%Y-%m-%d")


def display_name(addr_header):
    name, addr = parseaddr(addr_header)
    return (name or addr or addr_header).strip(), addr


def quote_mailbox(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_utf7_decode(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                b = s[i + 1:j].replace(",", "/")
                b += "=" * (-len(b) % 4)
                try:
                    out.append(base64.b64decode(b).decode("utf-16-be"))
                except Exception:
                    out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def parse_list_line(line):
    if isinstance(line, tuple):  # literal mailbox name
        head, lit = line
        line = head.decode("utf-8", "replace") + '"' + lit.decode("utf-8", "replace") + '"'
    elif isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    flags = m.group("flags").split()
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name, flags


# --------------------------------------------------------- body text -----

_ZW = dict.fromkeys(map(ord, "​‌‍⁠﻿­͏"), None)


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4",
             "h5", "h6", "blockquote", "section", "article", "header",
             "footer", "hr", "pre"}
    SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n• ")
        elif tag in ("td", "th"):
            self.out.append(" ")
        elif tag in self.BLOCK:
            self.out.append("\n\n" if tag in ("p", "h1", "h2", "h3") else "\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(src):
    p = _HTMLText()
    try:
        p.feed(src)
        p.close()
    except Exception:
        pass
    return "".join(p.out)


def clean_text(s):
    s = s.translate(_ZW).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = re.sub(r"\n[  ]+\n", "\n\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip("\n")


def _part_text(part):
    try:
        v = part.get_content()
        if isinstance(v, str):
            return v
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    cs = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(cs, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


class _HTMLBlocks(HTMLParser):
    """HTML -> paragraph blocks for the full view.

    Produces ('text', str, style) with style in normal/bold/dim/link, and
    ('img', src, alt) placeholders. Links get [n] markers and are listed at
    the end.
    """
    BLOCK = _HTMLText.BLOCK | {"li", "dd", "dt", "figure", "figcaption", "center"}
    SKIP = _HTMLText.SKIP
    HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, base=""):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.blocks, self.links, self.buf = [], [], []
        self.labels = {}
        self.skip = self.quote = self.head = 0
        self.href = None
        self.link_text = []
        self.title = ""
        self._in_title = False

    def _abs(self, url):
        url = (url or "").strip()
        if self.base and url and not url.lower().startswith(("data:", "cid:", "mailto:", "javascript:")):
            return urljoin(self.base, url)
        return url

    def flush(self):
        text = "".join(self.buf)
        self.buf = []
        lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
        text = "\n".join(ln for ln in lines if ln) if any(lines) else ""
        if text:
            style = "bold" if self.head else ("dim" if self.quote else "normal")
            self.blocks.append(("text", text, style))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "br":
            self.buf.append("\n")
        elif tag in ("td", "th"):
            self.buf.append(" ")
        elif tag == "img":
            src = (a.get("src") or "").strip()
            if not src or src.startswith("data:image/gif;base64,R0lGOD"):  # lazy-load placeholder
                lazy = a.get("data-src") or a.get("data-original") or ""
                srcset = (a.get("srcset") or a.get("data-srcset") or "").strip()
                src = (lazy or srcset.split(",")[0].split(" ")[0] or src).strip()
            w = (a.get("width") or "").replace("px", "").strip()
            h = (a.get("height") or "").replace("px", "").strip()
            style = (a.get("style") or "").replace(" ", "").lower()
            hidden = "display:none" in style or w in ("0", "1", "2") or h in ("0", "1", "2")
            if src and not hidden:
                self.flush()
                self.blocks.append(("img", self._abs(src), (a.get("alt") or "").strip()))
        elif tag == "a":
            self.href = self._abs(a.get("href"))
            self.link_text = []
        else:
            if tag in self.BLOCK or tag in self.HEAD:
                self.flush()
            if tag in self.HEAD:
                self.head += 1
            elif tag == "blockquote":
                self.quote += 1
            elif tag == "li":
                self.buf.append("• ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a":
            href, self.href = self.href, None
            text = re.sub(r"\s+", " ", "".join(self.link_text)).strip()
            if href and href.lower().startswith(("http://", "https://")):
                if href not in self.labels:
                    self.links.append(href)
                    self.labels[href] = text
                elif text and not self.labels[href]:
                    self.labels[href] = text
                if text != href:
                    self.buf.append(" [%d]" % (self.links.index(href) + 1))
        elif tag in self.BLOCK or tag in self.HEAD:
            self.flush()
            if tag in self.HEAD:
                self.head = max(0, self.head - 1)
            elif tag == "blockquote":
                self.quote = max(0, self.quote - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self.skip:
            return
        data = re.sub(r"\s+", " ", data.translate(_ZW))
        self.buf.append(data)
        if self.href is not None:
            self.link_text.append(data)

    def result(self):
        self.flush()
        out = self.blocks
        if self.links:
            out.append(("text", "Links", "bold"))
            for i, url in enumerate(self.links, 1):
                out.append(("text", "[%d] %s" % (i, url), "link"))
        return out


def parse_html(src, base=""):
    p = _HTMLBlocks(base)
    try:
        p.feed(src)
        p.close()
    except Exception:
        pass
    return p


def html_to_blocks(src):
    return parse_html(src).result()


_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+", re.I)


def find_urls(text):
    out = []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?")
        if u not in out:
            out.append(u)
    return out


class ImageRef:
    """An image shown in Full view, kept so it can be saved in full quality."""
    __slots__ = ("name", "data", "src", "size")

    def __init__(self, name, data, src, size):
        self.name, self.data, self.src, self.size = name, data, src, size


def _image_name(src, n):
    base = ""
    if src.lower().startswith(("http://", "https://")):
        base = os.path.basename(urlparse(src).path)
    elif src.lower().startswith("cid:"):
        base = src[4:].split("@")[0]
    return safe_filename(base, "image-%d" % n)


def decode_image(data, maxw=IMG_W, maxh=IMG_MAXH):
    """Bytes -> small RGB PIL image on white, or None."""
    try:
        from PIL import Image, ImageFile
        # Show what arrived of an image cut off at the size cap.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        im = Image.open(io.BytesIO(data))
        if im.format == "JPEG":
            im.draft("RGB", (maxw * 2, maxh * 2))
        im.seek(0)
        im = im.convert("RGBA")
        if im.width < 6 or im.height < 6:
            return None
        im.thumbnail((maxw, maxh))
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg
    except Exception:
        return None


def normalize_url(src):
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    return src.replace(" ", "%20")


def fetch_remote(url, limit=REMOTE_IMAGE_BYTES):
    req = urllib.request.Request(normalize_url(url), headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "image/png,image/jpeg,image/gif,image/webp,image/*;q=0.8,*/*;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(limit + 1)[:limit]


PAGE_LIMIT = 4 * 1024 * 1024
_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


_UA_ALT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
_cookies = None


def _opener():
    """Shared opener that keeps cookies, like a browser does across redirects."""
    global _cookies
    import http.cookiejar
    if _cookies is None:
        _cookies = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookies))


def _browser_headers(ua, accept):
    return {
        "User-Agent": ua,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


def _blocked_message(err):
    """Turn a refused request into something useful to show."""
    body = b""
    try:
        body = err.read(4096)
    except Exception:
        pass
    server = (err.headers.get("Server") or "").lower() if getattr(err, "headers", None) else ""
    if b"cf-chl" in body or b"Just a moment" in body or "cloudflare" in server:
        why = "this site only lets full browsers in (Cloudflare check)"
    elif err.code == 403:
        why = "the site refused the request (403 Forbidden)"
    elif err.code == 429:
        why = "the site says too many requests, try later"
    else:
        why = "the site answered %d %s" % (err.code, err.reason)
    return MailError(why + ". Press Q to open it on your phone.")


def _open_url(url, accept="*/*"):
    """GET with browser-like headers and cookies; retries once if refused."""
    import urllib.error
    url = normalize_url(url)
    last = None
    for ua in (_UA, _UA_ALT):
        req = urllib.request.Request(url, headers=_browser_headers(ua, accept))
        try:
            return _opener().open(req, timeout=15)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (403, 406, 429, 503):
                break
    raise _blocked_message(last)


def _cd_filename(headers):
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or \
        re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1).strip())
    return ""


class Page:
    """A fetched link: kind is html, text, image or file."""

    def __init__(self, url):
        self.url = url
        self.final = url
        self.kind = "file"
        self.title = ""
        self.ctype = ""
        self.filename = ""
        self.size = 0
        self.truncated = False
        self.data = b""
        self.blocks = []
        self.images = []
        self.links = []
        self.labels = {}


def fetch_page(url, remote=False):
    """Open a link for the built-in web view."""
    pg = Page(url)
    with _open_url(url, "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8") as r:
        pg.final = r.geturl()
        pg.ctype = (r.headers.get_content_type() or "").lower()
        charset = r.headers.get_content_charset()
        pg.filename = _cd_filename(r.headers)
        try:
            pg.size = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            pg.size = 0
        data = r.read(PAGE_LIMIT + 1)
    pg.truncated = len(data) > PAGE_LIMIT
    pg.data = data[:PAGE_LIMIT]
    pg.size = pg.size or len(pg.data)
    if not pg.filename:
        pg.filename = safe_filename(os.path.basename(urlparse(pg.final).path), "download")
    if pg.ctype in ("text/html", "application/xhtml+xml") or (
            not pg.ctype and pg.data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))):
        if not charset:
            m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', pg.data[:4096], re.I)
            charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
        try:
            html = pg.data.decode(charset, "replace")
        except LookupError:
            html = pg.data.decode("utf-8", "replace")
        pg.kind = "html"
        ph = parse_html(html, pg.final)
        pg.title = re.sub(r"\s+", " ", ph.title).strip()
        pg.labels = ph.labels
        pg.blocks, pg.images, pg.links = render_html(html, pg.final, remote)
    elif pg.ctype.startswith("text/"):
        pg.kind = "text"
        text = clean_text(pg.data.decode(charset or "utf-8", "replace"))
        pg.blocks = [("text", p, "normal") for p in text.split("\n\n") if p.strip()]
        pg.links = find_urls(text)
    elif pg.ctype.startswith("image/"):
        im = decode_image(pg.data)
        if im is not None:
            pg.kind = "image"
            pg.blocks = [("img", im)]
            pg.images = [ImageRef(pg.filename, pg.data, pg.final, im.size)]
    return pg


def download_url(url, dest_dir):
    """Save what a link points to into dest_dir. Returns the path."""
    with _open_url(url) as r:
        name = _cd_filename(r.headers) or os.path.basename(urlparse(r.geturl()).path)
        ctype = r.headers.get_content_type() or ""
        data = r.read(RAW_LIMIT + 1)
    if len(data) > RAW_LIMIT:
        raise MailError("File is larger than %d MB" % (RAW_LIMIT // (1024 * 1024)))
    name = safe_filename(name, "download")
    if "." not in name:
        import mimetypes
        ext = mimetypes.guess_extension(ctype) or (".html" if "html" in ctype else "")
        name += ext
    return save_bytes(data, dest_dir, name)


def image_error(data):
    """Why decode_image() failed, for the on-screen note and the log."""
    head = (data or b"")[:16]
    if not data:
        return "empty"
    if head.lstrip().startswith(b"<"):
        return "server sent a web page, not an image"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "AVIF not supported by this Pillow"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "WebP not supported by this Pillow"
    try:
        from PIL import Image
        Image.open(io.BytesIO(data)).load()
        return "too small (spacer)"
    except Exception as e:
        return "cannot decode: %s" % (str(e)[:60] or e.__class__.__name__)


def _fetch_many(urls):
    """Fetch several images in parallel. Returns {url: (bytes|None, error|None)}."""
    from concurrent.futures import ThreadPoolExecutor

    def one(u):
        try:
            return u, (fetch_remote(u), None)
        except Exception as e:
            reason = getattr(e, "reason", None) or getattr(e, "code", None) or e
            return u, (None, str(reason)[:70] or e.__class__.__name__)

    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, urls))


def _walk_parts(msg):
    """Leaf parts in a stable order; the index is used to find attachments again."""
    return [p for p in msg.walk() if not p.is_multipart()]


def extract(msg):
    """Returns (plain, html, [Attachment], {cid: bytes}, [image bytes not inline])."""
    plain = html = None
    atts, cids, loose = [], {}, []
    for idx, part in enumerate(_walk_parts(msg)):
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        fname = None
        try:
            fname = part.get_filename()
        except Exception:
            pass
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if ctype.startswith("image/") and cid and disp != "attachment":
            cids[cid] = part.get_payload(decode=True) or b""
            continue
        if disp == "attachment" or (fname and not ctype.startswith("text/")) \
                or not (ctype.startswith("text/") or ctype.startswith("image/")):
            payload = part.get_payload(decode=True) or b""
            atts.append(Attachment(idx, fname or ("attachment-%d" % idx), ctype, len(payload)))
            if ctype.startswith("image/"):
                loose.append((fname or ("image-%d" % idx), payload))
            continue
        if ctype.startswith("image/"):
            payload = part.get_payload(decode=True) or b""
            atts.append(Attachment(idx, fname or ("image-%d" % idx), ctype, len(payload)))
            loose.append((fname or ("image-%d" % idx), payload))
        elif ctype == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ctype == "text/html" and html is None:
            html = _part_text(part)
    return plain, html, atts, cids, loose


def count_images(html):
    """Visible <img> tags in an HTML body (tracking pixels excluded)."""
    if not html:
        return 0
    return sum(1 for b in html_to_blocks(html) if b[0] == "img")


def extract_body(msg):
    """Text view. Returns (text, [Attachment])."""
    plain, html, atts, _, _ = extract(msg)
    if plain and plain.strip():
        text = plain
    elif html:
        text = html_to_text(html)
    else:
        text = plain or ""
    return clean_text(text), atts


def build_blocks(msg, remote=False):
    """Full view of an email: returns (blocks, [ImageRef], [link urls])."""
    plain, html, _, cids, loose = extract(msg)
    if html:
        blocks, images, links = render_html(html, "", remote, cids)
    else:
        text = clean_text(plain or "")
        blocks = [("text", p, "dim" if p.startswith(">") else "normal")
                  for p in text.split("\n\n") if p.strip()]
        images, links = [], find_urls(text)
    for name, data in loose:
        im = decode_image(data)
        if im is not None:
            blocks.append(("img", im))
            images.append(ImageRef(name, data, "attachment", im.size))
    return blocks, images, links


def render_html(html, base="", remote=False, cids=None):
    """HTML -> (blocks, [ImageRef], [link urls]) with images decoded."""
    cids = cids or {}
    parsed = parse_html(html, base)
    raw_blocks = parsed.result()

    def is_remote(src):
        return src.lower().startswith(("http://", "https://", "//"))

    wanted = []
    if remote:
        for b in raw_blocks:
            if b[0] == "img" and is_remote(b[1]) and b[1] not in wanted:
                wanted.append(b[1])
        wanted = wanted[:MAX_REMOTE_IMAGES]
    fetched = _fetch_many(wanted)
    out, images, blocked, failures = [], [], 0, 0
    for b in raw_blocks:
        if b[0] != "img":
            out.append(b)
            continue
        src, alt = b[1], b[2]
        data, err = None, None
        if src.lower().startswith("cid:"):
            data = cids.get(src[4:].strip("<>"))
            err = None if data else "missing inline part"
        elif is_remote(src):
            if not remote:
                blocked += 1
                err = "blocked"
            elif src in fetched:
                data, err = fetched[src]
            else:
                err = "over the %d-image limit" % MAX_REMOTE_IMAGES
        elif src.lower().startswith("data:image/") and ";base64," in src:
            try:
                data = base64.b64decode(src.split(",", 1)[1])
            except Exception:
                err = "bad inline data"
        else:
            err = "unsupported source"
        im = decode_image(data) if data else None
        if im is not None:
            out.append(("img", im))
            images.append(ImageRef(_image_name(src, len(images) + 1), data, src, im.size))
            continue
        if data and not err:
            err = image_error(data)
        if err == "too small (spacer)":
            continue
        if err and err != "blocked":
            failures += 1
            log("image failed (%s): %s" % (err, src[:200]))
        label = alt if alt and len(alt) < 80 else "image"
        if err == "blocked":
            if alt and len(alt) < 120:
                out.append(("text", "[%s]" % alt, "dim"))
        elif failures <= 6:
            out.append(("text", "[%s: %s]" % (label, err), "dim"))
    if blocked:
        out.insert(0, ("text", "%d remote image%s not loaded (turn on in Setup)"
                       % (blocked, "s" if blocked != 1 else ""), "dim"))
    return out, images, parsed.links


def safe_filename(name, fallback="attachment"):
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", "_", name).strip(" .")
    return (name or fallback)[:120]


def save_attachment(raw, idx, dest_dir, name):
    """Write leaf part idx of raw message bytes into dest_dir. Returns path."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    parts = _walk_parts(msg)
    if not 0 <= idx < len(parts):
        raise MailError("Attachment not found")
    data = parts[idx].get_payload(decode=True) or b""
    return save_bytes(data, dest_dir, name)


def save_bytes(data, dest_dir, name):
    """Write data to dest_dir/name without overwriting. Returns the path."""
    if "." not in name:
        try:
            from PIL import Image
            fmt = Image.open(io.BytesIO(data)).format
            if fmt:
                name += "." + {"JPEG": "jpg"}.get(fmt, fmt.lower())
        except Exception:
            pass
    created = not os.path.isdir(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    base, ext = os.path.splitext(safe_filename(name))
    path = os.path.join(dest_dir, base + ext)
    n = 1
    while os.path.exists(path):
        path = os.path.join(dest_dir, "%s (%d)%s" % (base, n, ext))
        n += 1
    with open(path, "wb") as fh:
        fh.write(data)
    _give_to_owner(dest_dir, path, created)
    return path


def _give_to_owner(dest_dir, path, created):
    """APPLaunch runs us as root; hand files to whoever owns the home dir."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        st = os.stat(os.path.dirname(os.path.abspath(dest_dir)))
        if st.st_uid == 0:
            return
        os.chown(path, st.st_uid, st.st_gid)
        if created:
            os.chown(dest_dir, st.st_uid, st.st_gid)
    except OSError:
        pass


def parse_message(uid, raw, truncated=False, full=False, remote=False):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    m = Message(uid)
    m.sender = _hdr(msg, "From")
    m.to = _hdr(msg, "To")
    m.cc = _hdr(msg, "Cc")
    m.reply_to = _hdr(msg, "Reply-To")
    m.subject = _hdr(msg, "Subject")
    m.date_raw = _hdr(msg, "Date")
    m.date = _date(m.date_raw)
    m.message_id = _hdr(msg, "Message-ID")
    m.references = _hdr(msg, "References")
    try:
        m.text, m.attachments = extract_body(msg)
        _, html, _, _, loose = extract(msg)
        if html:
            ph = parse_html(html)
            m.links, m.link_labels = ph.links, ph.labels
            m.image_count = count_images(html) + len(loose)
        else:
            m.links = find_urls(m.text)
            m.image_count = len(loose)
    except Exception as e:  # never crash the reader on odd MIME
        m.text = "(could not decode message: %s)" % e
    if full:
        try:
            m.blocks, m.images, links = build_blocks(msg, remote)
            m.links = links or m.links
        except Exception as e:
            m.blocks = [("text", "(could not render: %s)" % e, "dim"), ("text", m.text, "normal")]
        m.full = True
    m.truncated = truncated
    if not truncated:
        m.raw = raw
    return m


def parse_summaries(data):
    """Parse a UID FETCH (UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ...]) reply."""
    out = []
    cur = None

    def apply_meta(rec, meta):
        mu = re.search(r"UID (\d+)", meta)
        if mu:
            rec.uid = int(mu.group(1))
        mf = re.search(r"FLAGS \(([^)]*)\)", meta)
        if mf:
            rec.seen = "\\Seen" in mf.group(1).split()
        ms = re.search(r"RFC822\.SIZE (\d+)", meta)
        if ms:
            rec.size = int(ms.group(1))

    for item in data:
        if isinstance(item, tuple):
            meta = item[0].decode("utf-8", "replace")
            cur = Summary(0, seen=False)
            apply_meta(cur, meta)
            hdr = BytesParser(policy=policy.default).parsebytes(item[1], headersonly=True)
            frm = _hdr(hdr, "From")
            cur.sender, cur.sender_addr = display_name(frm)
            cur.subject = _hdr(hdr, "Subject") or "(no subject)"
            cur.date = _date(_hdr(hdr, "Date"))
            out.append(cur)
        elif isinstance(item, bytes) and cur is not None:
            apply_meta(cur, item.decode("utf-8", "replace"))
    out = [s for s in out if s.uid]
    out.sort(key=lambda s: s.uid, reverse=True)
    return out


# ------------------------------------------------------------- IMAP ------

class ImapClient:
    """Thin stateful wrapper. Use from a single (worker) thread only."""

    def __init__(self, acct):
        self.acct = acct
        self.conn = None
        self.selected = None
        self.special = {}  # '\\Sent' -> mailbox name, '\\Trash' -> ...

    # connection ---------------------------------------------------------
    def connect(self):
        a = self.acct
        host = a["imap_host"].strip()
        port = int(a.get("imap_port") or 993)
        ctx = _ssl_ctx(host)
        try:
            if port == 993 or (port != 143 and not _is_local(host)):
                c = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=TIMEOUT)
            else:
                c = imaplib.IMAP4(host, port, timeout=TIMEOUT)
                if "STARTTLS" in c.capabilities:
                    c.starttls(ssl_context=ctx)
            c.login(a.get("username") or a["email"], a["password"])
        except imaplib.IMAP4.error as e:
            raise MailError("IMAP login failed: %s" % _msg(e))
        except (OSError, ssl.SSLError) as e:
            raise MailError("IMAP connect failed: %s" % _msg(e))
        self.conn = c
        self.selected = None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
        self.conn = None
        self.selected = None

    def _run(self, fn):
        """Run fn() with one reconnect attempt on a dropped connection."""
        if self.conn is None:
            self.connect()
        try:
            return fn()
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError, socket.timeout):
            self.close()
            self.connect()
            return fn()

    def _select(self, folder):
        if self.selected == folder:
            return
        typ, data = self.conn.select(quote_mailbox(folder))
        if typ != "OK":
            raise MailError("Cannot open folder %s" % folder)
        self.selected = folder

    # operations ---------------------------------------------------------
    def folders(self):
        def op():
            typ, data = self.conn.list()
            if typ != "OK":
                raise MailError("LIST failed")
            out = []
            for line in data:
                if line is None:
                    continue
                parsed = parse_list_line(line)
                if not parsed:
                    continue
                name, flags = parsed
                for sp in ("\\Sent", "\\Trash", "\\Drafts", "\\Junk", "\\All"):
                    if sp in flags:
                        self.special[sp] = name
                if "\\Noselect" in flags or "\\NonExistent" in flags:
                    continue
                out.append((name, imap_utf7_decode(name)))
            out.sort(key=lambda x: (x[0].upper() != "INBOX", x[1].lower()))
            return out
        return self._run(op)

    def search_uids(self, folder):
        def op():
            self.selected = None  # re-select to see new mail
            self._select(folder)
            typ, data = self.conn.uid("SEARCH", None, "ALL")
            if typ != "OK":
                raise MailError("SEARCH failed")
            return sorted(int(x) for x in (data[0] or b"").split())
        return self._run(op)

    def search(self, folder, query, all_mail=False):
        """Server-side search. Returns (folder_searched, sorted uids).

        Gmail: Gmail's own search syntax (X-GM-RAW). Others: IMAP TEXT,
        which matches headers (From, Subject...) and body.
        """
        def op():
            target = folder
            if all_mail:
                if not self.special:
                    self.folders()
                target = self.special.get("\\All", folder)
            self.selected = None
            self._select(target)
            q = query.strip().encode("utf-8")
            gmail = "X-GM-EXT-1" in self.conn.capabilities
            attempts = ([("CHARSET", "UTF-8", "X-GM-RAW")] if gmail else []) + \
                [("CHARSET", "UTF-8", "TEXT"), ("TEXT",)]
            last = None
            for args in attempts:
                try:
                    self.conn.literal = q
                    typ, data = self.conn.uid("SEARCH", *args)
                    if typ == "OK":
                        return target, sorted(int(x) for x in (data[0] or b"").split())
                    last = data
                except imaplib.IMAP4.error as e:
                    last = e
            raise MailError("Search failed: %s" % _msg(last) if last else "Search failed")
        return self._run(op)

    def summaries(self, folder, uids):
        if not uids:
            return []

        def op():
            self._select(folder)
            spec = ",".join(str(u) for u in uids)
            typ, data = self.conn.uid(
                "FETCH", spec,
                "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK":
                raise MailError("FETCH failed")
            return parse_summaries(data)
        return self._run(op)

    def fetch_raw(self, folder, uid):
        """Whole message (for saving attachments)."""
        def op():
            self._select(folder)
            typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[]<0.%d>)" % RAW_LIMIT)
            if typ != "OK":
                raise MailError("FETCH failed")
            for item in data:
                if isinstance(item, tuple):
                    return item[1]
            raise MailError("Message not found")
        return self._run(op)

    def scan_sent(self, last_uid=0, folder_hint="", limit=500):
        """Recipients from the Sent folder newer than last_uid.

        Returns (sent_folder, newest_uid, [(name, addr, datetime)]).
        """
        def op():
            if not self.special:
                self.folders()
            sent = self.special.get("\\Sent") or folder_hint
            if not sent:
                return "", 0, []
            start = last_uid if sent == folder_hint else 0
            self.selected = None
            self._select(sent)
            typ, data = self.conn.uid("SEARCH", None, "UID %d:*" % (start + 1))
            if typ != "OK":
                return sent, start, []
            uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > start)
            if not uids:
                return sent, start, []
            uids = uids[-limit:]
            out = []
            for i in range(0, len(uids), 150):
                chunk = ",".join(str(u) for u in uids[i:i + 150])
                typ, rows = self.conn.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (TO CC DATE)])")
                if typ != "OK":
                    continue
                for item in rows:
                    if not isinstance(item, tuple):
                        continue
                    hdr = BytesParser(policy=policy.default).parsebytes(item[1], headersonly=True)
                    when = _date(_hdr(hdr, "Date"))
                    for name, addr in getaddresses([_hdr(hdr, "To"), _hdr(hdr, "Cc")]):
                        if "@" in addr:
                            out.append((name, addr, when))
            self.selected = None
            return sent, uids[-1], out
        return self._run(op)

    def message(self, folder, uid, mark_seen=True, full=False, remote=False):
        def op():
            self._select(folder)
            limit = FETCH_LIMIT_FULL if full else FETCH_LIMIT
            typ, data = self.conn.uid(
                "FETCH", str(uid), "(RFC822.SIZE BODY.PEEK[]<0.%d>)" % limit)
            if typ != "OK":
                raise MailError("FETCH failed")
            raw, size = b"", 0
            for item in data:
                if isinstance(item, tuple):
                    raw = item[1]
                    ms = re.search(rb"RFC822\.SIZE (\d+)", item[0])
                    if ms:
                        size = int(ms.group(1))
                elif isinstance(item, bytes):
                    ms = re.search(rb"RFC822\.SIZE (\d+)", item)
                    if ms:
                        size = int(ms.group(1))
            if not raw:
                raise MailError("Message not found")
            if mark_seen:
                self.conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
            return parse_message(uid, raw, truncated=size > len(raw), full=full, remote=remote)
        return self._run(op)

    def set_seen(self, folder, uid, seen):
        def op():
            self._select(folder)
            self.conn.uid("STORE", str(uid), "+FLAGS" if seen else "-FLAGS", "(\\Seen)")
        return self._run(op)

    def delete(self, folder, uid):
        """Move to Trash when the server has one, else flag + expunge."""
        def op():
            if not self.special:
                try:
                    self.folders()
                except MailError:
                    pass
            self._select(folder)
            trash = self.special.get("\\Trash")
            if trash and trash != folder:
                if "MOVE" in self.conn.capabilities:
                    typ, _ = self.conn.uid("MOVE", str(uid), quote_mailbox(trash))
                    if typ == "OK":
                        return
                self.conn.uid("COPY", str(uid), quote_mailbox(trash))
            self.conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
            self.conn.expunge()
        return self._run(op)

    def append_sent(self, msg_bytes):
        """Save a copy to Sent, except where the server does it already."""
        host = self.acct.get("smtp_host", "").lower()
        if "gmail" in host or "office365" in host or "outlook" in host:
            return False

        def op():
            if not self.special:
                self.folders()
            sent = self.special.get("\\Sent")
            if not sent:
                return False
            self.conn.append(quote_mailbox(sent), "(\\Seen)",
                             imaplib.Time2Internaldate(_dt.datetime.now().astimezone()),
                             msg_bytes)
            return True
        return self._run(op)


def _msg(e):
    s = e.args[0] if getattr(e, "args", None) else e
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    return str(s)


# ------------------------------------------------------------- SMTP ------

def build_message(acct, to, cc, subject, body, in_reply_to="", references=""):
    msg = EmailMessage()
    sender = acct["email"].strip()
    msg["From"] = formataddr((acct.get("name") or "", sender)) if acct.get("name") else sender
    to = ", ".join(split_recipients(to))   # drop the trailing ", " a picked contact leaves
    cc = ", ".join(split_recipients(cc))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = sender.split("@", 1)[-1] if "@" in sender else None
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    msg["User-Agent"] = "CZ Email (Cardputer Zero)"
    msg.set_content(body)
    return msg


def getaddresses(values):
    """[(name, addr)] from header/field strings, one address at a time.

    Python 3.13+ made email.utils.getaddresses strict: one empty entry (an
    empty Cc, a trailing comma) makes it return nothing at all. Splitting
    first and parsing each address on its own avoids that.
    """
    out = []
    for value in values:
        for token in split_recipients(str(value or "")):
            name, addr = parseaddr(token)
            if addr:
                out.append((name, addr))
    return out


def recipients(to, cc=""):
    return [a for _, a in getaddresses([to, cc]) if "@" in a]


_ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def split_recipients(value):
    """Split 'a, "Doe, J" <j@x>, b' on commas outside quotes and <>."""
    out, cur, quote, angle = [], [], False, False
    for ch in value:
        if ch == '"':
            quote = not quote
        elif ch == "<":
            angle = True
        elif ch == ">":
            angle = False
        if ch in ",;" and not quote and not angle:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [t for t in out if t]


def is_address(token):
    return bool(_ADDR_RE.match(parseaddr(token)[1] or ""))


def smtp_send(acct, msg, test_only=False):
    host = acct["smtp_host"].strip()
    port = int(acct.get("smtp_port") or 465)
    ctx = _ssl_ctx(host)
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT)
        else:
            s = smtplib.SMTP(host, port, timeout=TIMEOUT)
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ctx)
                s.ehlo()
            elif not _is_local(host):
                s.quit()
                raise MailError("SMTP server offers no encryption on port %d" % port)
        try:
            s.login(acct.get("username") or acct["email"], acct["password"])
            if not test_only:
                rcpts = recipients(msg.get("To", ""), msg.get("Cc", ""))
                if not rcpts:
                    raise MailError("No valid recipients")
                s.send_message(msg, to_addrs=rcpts)
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as e:
        raise MailError("SMTP login failed: %s" % _msg(e.smtp_error if hasattr(e, "smtp_error") else e))
    except smtplib.SMTPException as e:
        raise MailError("SMTP error: %s" % _msg(e))
    except (OSError, ssl.SSLError) as e:
        raise MailError("SMTP connect failed: %s" % _msg(e))


# ------------------------------------------------------ reply helpers ----

def _prefixed(subject, prefix):
    s = subject.strip()
    if s.lower().startswith(prefix.lower()):
        return s
    return prefix + " " + s


def reply_fields(acct, m, reply_all=False):
    to = m.reply_to or m.sender
    cc = ""
    if reply_all:
        me = acct.get("email", "").lower()
        mine = {a.lower() for a in recipients(to)} | {me}
        others = []
        for name, addr in getaddresses([m.to, m.cc]):
            if "@" in addr and addr.lower() not in mine:
                mine.add(addr.lower())
                others.append(formataddr((name, addr)) if name else addr)
        cc = ", ".join(others)
    who = display_name(m.sender)[0]
    when = m.date.strftime("%a %d %b %Y %H:%M") if m.date else m.date_raw
    quoted = "\n".join("> " + line for line in m.text.split("\n"))
    body = "\n\nOn %s, %s wrote:\n%s\n" % (when, who, quoted)
    return to, cc, _prefixed(m.subject, "Re:"), body


def forward_fields(m):
    head = ("\n\n---------- Forwarded message ----------\n"
            "From: %s\nDate: %s\nSubject: %s\nTo: %s\n\n" %
            (m.sender, m.date_raw, m.subject, m.to))
    return "", "", _prefixed(m.subject, "Fwd:"), head + m.text + "\n"
