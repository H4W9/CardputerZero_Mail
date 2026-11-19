"""Account settings, saved as JSON with owner-only permissions."""

import json
import os

import glob

FIELDS = ("name", "email", "username", "password", "imap_host", "imap_port",
          "smtp_host", "smtp_port", "view_mode", "remote_images", "download_dir")

VIEW_MODES = ("text", "full")       # text only / formatted with images
ON_OFF = ("off", "on")

# domain -> (imap_host, imap_port, smtp_host, smtp_port)
PRESETS = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "outlook.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    "hotmail.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    "live.com": ("outlook.office365.com", 993, "smtp-mail.outlook.com", 587),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "fastmail.com": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465),
    "gmx.com": ("imap.gmx.com", 993, "mail.gmx.com", 587),
    "gmx.de": ("imap.gmx.net", 993, "mail.gmx.net", 587),
    "web.de": ("imap.web.de", 993, "smtp.web.de", 587),
    "aol.com": ("imap.aol.com", 993, "smtp.aol.com", 465),
    "zoho.com": ("imap.zoho.com", 993, "smtp.zoho.com", 465),
    "proton.me": ("127.0.0.1", 1143, "127.0.0.1", 1025),  # Proton Bridge
}


def config_path():
    p = os.environ.get("CZMAIL_CONFIG")
    if p:
        return p
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "czmail", "config.json")


def empty_account():
    return {"name": "", "email": "", "username": "", "password": "",
            "imap_host": "", "imap_port": "993", "smtp_host": "", "smtp_port": "465",
            "view_mode": "text", "remote_images": "off", "download_dir": ""}


def default_download_dir():
    """The device user's ~/Downloads (APPLaunch runs us as root)."""
    homes = sorted(h for h in glob.glob("/home/*") if os.path.isdir(h))
    for h in homes:
        if os.path.isdir(os.path.join(h, "Downloads")):
            return os.path.join(h, "Downloads")
    if homes:
        return os.path.join(homes[0], "Downloads")
    return os.path.join(os.path.expanduser("~"), "Downloads")


def download_dir(acct):
    return (acct.get("download_dir") or "").strip() or default_download_dir()


def preset_for(email):
    domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
    return PRESETS.get(domain)


def apply_preset(acct):
    """Fill empty server fields from the email domain. Returns True if changed."""
    p = preset_for(acct.get("email", ""))
    if not p or acct.get("imap_host"):
        return False
    acct["imap_host"], acct["imap_port"] = p[0], str(p[1])
    acct["smtp_host"], acct["smtp_port"] = p[2], str(p[3])
    return True


def is_complete(acct):
    return bool(acct and acct.get("email") and acct.get("password")
                and acct.get("imap_host") and acct.get("smtp_host"))


def load():
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty_account()
    acct = empty_account()
    for k in FIELDS:
        if k in data and data[k] is not None:
            acct[k] = str(data[k])
    return acct


def save(acct):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({k: acct.get(k, "") for k in FIELDS}, fh, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
