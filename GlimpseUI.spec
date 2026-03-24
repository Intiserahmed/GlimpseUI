# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for GlimpseUI desktop app.
Build: python -m PyInstaller GlimpseUI.spec
"""
import os
import sys
from pathlib import Path

block_cipher = None
ROOT = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(ROOT, 'seer_app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # API keys / config (seeded to ~/.glimpseui/.env on first launch)
        (os.path.join(ROOT, '.env'), '.'),
        # Web UI
        (os.path.join(ROOT, 'static'), 'static'),
        # Agent logic
        (os.path.join(ROOT, 'agent'), 'agent'),
        # Client scripts (spawned as subprocesses by main.py)
        (os.path.join(ROOT, 'clients'), 'clients'),
        # Main FastAPI app (run as subprocess by seer_app.py)
        (os.path.join(ROOT, 'main.py'), '.'),
        # xctest-bridge build script (for iOS)
        (os.path.join(ROOT, 'xctest-bridge'), 'xctest-bridge'),
    ],
    hiddenimports=[
        # uvicorn internals
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # FastAPI / Starlette
        'starlette.routing',
        'starlette.middleware',
        'starlette.middleware.cors',
        # Pydantic
        'pydantic',
        'pydantic.deprecated.class_validators',
        # pywebview macOS backend
        'webview.platforms.cocoa',
        # google-genai
        'google.genai',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy unused packages
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'IPython', 'jupyter', 'notebook',
        'tkinter', 'wx',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GlimpseUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=os.path.join(ROOT, 'GlimpseUI.entitlements'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='GlimpseUI',
)

app = BUNDLE(
    coll,
    name='GlimpseUI.app',
    icon=None,              # swap for 'assets/icon.icns' when you have one
    bundle_identifier='com.glimpseui.app',
    info_plist={
        'CFBundleName': 'GlimpseUI',
        'CFBundleDisplayName': 'GlimpseUI',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.3',
        'NSAppleEventsUsageDescription': 'GlimpseUI needs Accessibility access to control apps.',
        'NSAccessibilityUsageDescription': 'GlimpseUI needs Accessibility access to control apps.',
        'NSScreenCaptureUsageDescription': 'GlimpseUI needs Screen Recording to show the live iOS Simulator view.',
        'NSAppTransportSecurity': {
            'NSAllowsLocalNetworking': True,
            'NSAllowsArbitraryLoads': True,
        },
    },
)
