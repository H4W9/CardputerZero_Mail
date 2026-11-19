#!/usr/bin/env bash
# Called by AppBuilder's scripts/pack-deb.sh with STAGE, INSTALL_PREFIX,
# APP_INSTALL_DIR and PKG_NAME exported.
set -euo pipefail

for f in czmail/*.py; do
    install -D -m 0644 "$f" "$STAGE$APP_INSTALL_DIR/$f"
done
install -D -m 0644 packaging/icon.png "$STAGE$INSTALL_PREFIX/share/images/czmail.png"
install -D -m 0755 packaging/run-czmail "$STAGE$INSTALL_PREFIX/bin/czmail"
install -D -m 0755 packaging/install_speech.sh "$STAGE$APP_INSTALL_DIR/install_speech.sh"
