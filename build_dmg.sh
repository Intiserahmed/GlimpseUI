#!/bin/bash
# Build GlimpseUI.app and package as DMG
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="GlimpseUI"
VERSION="1.0.0"
DIST_DIR="$ROOT/dist"
DMG_NAME="${APP_NAME}-${VERSION}.dmg"

echo "==> Building $APP_NAME $VERSION"

# ── 1. Run PyInstaller ────────────────────────────────────────────────────────
cd "$ROOT"
"$ROOT/venv/bin/python" -m PyInstaller GlimpseUI.spec --noconfirm --clean
echo "==> Build complete: $DIST_DIR/$APP_NAME.app"

# ── 2. Package as DMG ─────────────────────────────────────────────────────────
echo "==> Creating DMG…"

if command -v create-dmg &>/dev/null; then
    # Nice drag-to-Applications DMG via create-dmg (brew install create-dmg)
    create-dmg \
        --volname "$APP_NAME $VERSION" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "${APP_NAME}.app" 150 185 \
        --hide-extension "${APP_NAME}.app" \
        --app-drop-link 450 185 \
        --no-internet-enable \
        "$DIST_DIR/$DMG_NAME" \
        "$DIST_DIR/${APP_NAME}.app"
else
    # Fallback: plain hdiutil DMG (no fancy layout)
    echo "  (create-dmg not found — using hdiutil. Run: brew install create-dmg for a nicer DMG)"
    hdiutil create \
        -volname "$APP_NAME $VERSION" \
        -srcfolder "$DIST_DIR/${APP_NAME}.app" \
        -ov -format UDZO \
        "$DIST_DIR/$DMG_NAME"
fi

echo ""
echo "✅ Done: $DIST_DIR/$DMG_NAME"
echo "   To install: open $DIST_DIR/$DMG_NAME and drag to Applications"
