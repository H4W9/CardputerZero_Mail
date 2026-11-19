# Email for Cardputer Zero

An email app for the M5Stack Cardputer Zero. Shows up in the launcher as Email.

![inbox](store/screenshots/inbox.png) ![full view](store/screenshots/reader-full.png)
![compose](store/screenshots/compose.png) ![setup](store/screenshots/setup.png)

## What it does

- Inbox, folders, search
- Read, reply, reply all, forward, delete
- Contact suggestions from people you've emailed
- Text only or full view with pictures (V switches)
- Save attachments and pictures to ~/Downloads
- Open links in a simple text browser, show them as a QR code, or download them
- Voice typing that works offline (optional, see below)

Press Fn+H on any screen to see its keys.

## Install

Grab the .deb from the releases page (or the App Store once it's listed), copy it to the Cardputer and run:

```bash
sudo apt install ./czmail_0.3.3-m5stack1_all.deb
```

The app shows up in the launcher. The first time you open it, enter your email address and password. Server settings fill in by themselves for Gmail, Outlook, Yahoo, iCloud, Fastmail, GMX, AOL and Zoho. For anything else, type in your IMAP and SMTP servers.

Gmail, Yahoo, iCloud and Outlook need an app password instead of your normal one. For Gmail: turn on 2-Step Verification, then make one at https://myaccount.google.com/apppasswords

Your settings are saved in /root/.config/czmail/ (the launcher runs apps as root), readable by root only. The password is stored as plain text in that file.

## Keys

| Screen | Keys |
|---|---|
| Anywhere | Esc back, hold Esc to quit, Fn+H (or H, ?) help, Fn + F X Z C for arrows |
| Inbox | Enter open, / search, C new, R refresh, F folders, D delete, U unread, S settings |
| Email | R reply, A reply all, F forward, V view, I pictures, L links, T attachments, D delete |
| Writing | Tab next field, Ctrl+S send, Ctrl+Space voice typing, Ctrl+K contacts |
| Links | Enter open, Q QR code, S download |

## Voice typing

Uses Vosk with the small English model (about 45 MB). Everything stays on the device. Install it once:

```bash
sudo sh /usr/share/APPLaunch/apps/czmail/install_speech.sh
```

You need a microphone that shows up in `arecord -l`. While writing, press Ctrl+Space or Fn+Space, talk, then press Enter. Say "comma", "period", "question mark" or "new line" for punctuation.

## Problems

Images or links that don't load are logged to /tmp/czmail.log. To test a single image:

```bash
cd /usr/share/APPLaunch/apps/czmail && python3 -m czmail --image-test <url>
```

## Building

Plain Python 3 with Pillow, no compiling needed.

```bash
python tools/build_deb.py                 # makes dist/czmail_<version>_all.deb on any OS
python -m czmail --demo                   # try it in a window on your PC (F1 = help)
python -m unittest discover -s tests      # tests
```

It also builds with the CardputerZero AppBuilder (packaging/meta.env, build.sh, stage.sh).

## License

MIT
