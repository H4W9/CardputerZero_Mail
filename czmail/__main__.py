"""Entry point: python3 -m czmail [--demo] [--window] [--screenshots DIR]"""

import argparse
import sys

from . import __version__


def main(argv=None):
    p = argparse.ArgumentParser(prog="czmail", description="CZ Email for Cardputer Zero")
    p.add_argument("--demo", action="store_true", help="offline demo mailbox")
    p.add_argument("--window", action="store_true", help="force the desktop preview window")
    p.add_argument("--scale", type=int, default=3, help="desktop window zoom (default 3)")
    p.add_argument("--screenshots", metavar="DIR", help="render store screenshots and exit")
    p.add_argument("--image-test", metavar="URL",
                   help="download and decode one image, print what happened")
    p.add_argument("--keytest", action="store_true",
                   help="print what the keyboard sends (run over SSH, press keys on the device)")
    p.add_argument("--ext", action="store_true",
                   help="use the 320x240 EXT display layout (desktop preview, screenshots)")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    # The screen size has to be known before app/display are imported.
    from . import ui
    ext_path = None
    if args.ext:
        ui.set_size(320, 240)
    elif not args.window and not args.screenshots:
        from . import config, extdisplay
        ext_path = extdisplay.choose(config.load().get("screen", "auto"))
        if ext_path:
            ui.set_size(extdisplay.EXT_W, extdisplay.EXT_H)

    if args.image_test:
        return image_test(args.image_test)

    if args.keytest:
        from .keyboard import EvdevKeyboard
        kb = EvdevKeyboard()
        try:
            kb.keytest()
        except KeyboardInterrupt:
            pass
        finally:
            kb.close()
        return 0

    if args.screenshots:
        from .demo import screenshots
        for path in screenshots(args.screenshots):
            print(path)
        return 0

    if args.demo:
        from .demo import make_demo_app
        app = make_demo_app()
    else:
        from .app import App
        app = App()

    from .display import device_available, run_desktop, run_device
    if args.window or not device_available():
        run_desktop(app, scale=args.scale)
    else:
        run_device(app, ext_path=ext_path)
    return 0


def image_test(url):
    import PIL
    from PIL import features

    from . import mailer
    import warnings
    warnings.simplefilter("ignore")

    def has(name):
        try:
            return bool(features.check(name))
        except Exception:
            return False

    print("Pillow", PIL.__version__, "| webp:", has("webp"), "| avif:", has("avif"))
    try:
        data = mailer.fetch_remote(url)
    except Exception as e:
        print("DOWNLOAD FAILED:", getattr(e, "reason", None) or getattr(e, "code", None) or e)
        return 1
    print("downloaded %d bytes, starts with %r" % (len(data), data[:12]))
    im = mailer.decode_image(data)
    if im is None:
        print("DECODE FAILED:", mailer.image_error(data))
        return 1
    print("OK: decoded, shown at %dx%d" % im.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
