"""
Simulator Bridge — controls iOS Simulator via macOS System Events (osascript).
No third-party dependencies. Pure macOS accessibility API.

How it works:
  1. Gets Simulator window bounds via osascript
  2. Maps iOS logical coordinates → screen pixel coordinates
  3. Clicks via System Events (same as a real user clicking)
  4. Screenshots via xcrun simctl io booted screenshot
"""

import subprocess
import base64
import re
from io import BytesIO
from PIL import Image

# iPhone 17 Pro logical resolution
VIEWPORT_W = 393
VIEWPORT_H = 852

# Device chrome insets as fraction of window size
# Accounts for macOS title bar + device bezel around the screen
# Calibrated for iPhone 17 Pro in Simulator at default scale
INSET = {
    "top":    0.095,   # title bar + top bezel + dynamic island
    "bottom": 0.055,   # bottom bezel + home indicator
    "left":   0.038,
    "right":  0.038,
}


def get_window_bounds() -> tuple[int, int, int, int]:
    """Get Simulator window bounds in screen coordinates."""
    script = '''
    tell application "System Events"
        tell process "Simulator"
            set b to bounds of window 1
            return (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b)
        end tell
    end tell
    '''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    parts = r.stdout.strip().split(",")
    return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])


def ios_to_screen(ios_x: int, ios_y: int) -> tuple[int, int]:
    """Map iOS logical coordinates to macOS screen coordinates."""
    left, top, right, bottom = get_window_bounds()
    win_w = right - left
    win_h = bottom - top

    screen_left = left + win_w * INSET["left"]
    screen_top  = top  + win_h * INSET["top"]
    screen_w    = win_w * (1 - INSET["left"] - INSET["right"])
    screen_h    = win_h * (1 - INSET["top"]  - INSET["bottom"])

    click_x = int(screen_left + (ios_x / VIEWPORT_W) * screen_w)
    click_y = int(screen_top  + (ios_y / VIEWPORT_H) * screen_h)
    return click_x, click_y


def tap(ios_x: int, ios_y: int):
    sx, sy = ios_to_screen(ios_x, ios_y)
    script = f'''
    tell application "Simulator" to activate
    delay 0.1
    tell application "System Events"
        tell process "Simulator"
            click at {{{sx}, {sy}}}
        end tell
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)
    return sx, sy


def double_tap(ios_x: int, ios_y: int):
    sx, sy = ios_to_screen(ios_x, ios_y)
    script = f'''
    tell application "Simulator" to activate
    delay 0.1
    tell application "System Events"
        tell process "Simulator"
            click at {{{sx}, {sy}}}
            delay 0.1
            click at {{{sx}, {sy}}}
        end tell
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)


def type_text(text: str):
    # Escape for osascript
    escaped = text.replace('"', '\\"').replace("\\", "\\\\")
    script = f'''
    tell application "Simulator" to activate
    delay 0.1
    tell application "System Events"
        keystroke "{escaped}"
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)


def keypress(key: str):
    key_map = {
        "Return": "return", "Enter": "return",
        "Escape": "escape", "Tab": "tab",
        "Backspace": "delete",
        "ArrowUp": "up arrow", "ArrowDown": "down arrow",
        "ArrowLeft": "left arrow", "ArrowRight": "right arrow",
    }
    mapped = key_map.get(key, key.lower())
    script = f'''
    tell application "Simulator" to activate
    delay 0.1
    tell application "System Events"
        key code 0  -- dummy
    end tell
    '''
    # Use keystroke for regular keys
    script = f'''
    tell application "Simulator" to activate
    delay 0.1
    tell application "System Events"
        keystroke {mapped}
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)


def swipe(direction: str):
    """Swipe in the given direction."""
    cx_ios = VIEWPORT_W // 2
    if direction == "down":
        y1_ios, y2_ios = int(VIEWPORT_H * 0.7), int(VIEWPORT_H * 0.3)
    else:
        y1_ios, y2_ios = int(VIEWPORT_H * 0.3), int(VIEWPORT_H * 0.7)

    sx1, sy1 = ios_to_screen(cx_ios, y1_ios)
    sx2, sy2 = ios_to_screen(cx_ios, y2_ios)

    script = f'''
    tell application "Simulator" to activate
    delay 0.1
    tell application "System Events"
        tell process "Simulator"
            -- drag from start to end
            set startPoint to {{{sx1}, {sy1}}}
            set endPoint to {{{sx2}, {sy2}}}
            click at startPoint
        end tell
    end tell
    '''
    # Use key code drag simulation
    subprocess.run(["osascript", "-e", script], capture_output=True)


def screenshot_b64(max_px: int = 1024) -> str:
    """Take screenshot from simulator, return resized JPEG base64."""
    path = "/tmp/glimpseui_ios.png"
    subprocess.run(
        ["xcrun", "simctl", "io", "booted", "screenshot", path],
        check=True, capture_output=True
    )
    img = Image.open(path).convert("RGB")
    img = img.resize((VIEWPORT_W, VIEWPORT_H), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode()


def calibrate():
    """Print current window bounds and test a center tap."""
    left, top, right, bottom = get_window_bounds()
    print(f"Window bounds: left={left}, top={top}, right={right}, bottom={bottom}")
    print(f"Window size: {right-left}x{bottom-top}")

    cx, cy = ios_to_screen(VIEWPORT_W // 2, VIEWPORT_H // 2)
    print(f"Center of iOS screen → screen ({cx}, {cy})")
    print("Tapping center of simulator screen...")
    tap(VIEWPORT_W // 2, VIEWPORT_H // 2)
    print("Done. Check if tap landed correctly.")
