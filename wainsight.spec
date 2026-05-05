# wainsight.spec  — hand-crafted for WAInsight
# Build with: pyinstaller wainsight.spec --noconfirm

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ── Data files that must travel with the EXE ────────────────────────────────
datas = []

# qt-material ships theme QSS + icon data — collect all of it
datas += collect_data_files("qt_material")

# openpyxl needs its template xlsx files
datas += collect_data_files("openpyxl")

# protobuf may need its descriptor pool data
datas += collect_data_files("google.protobuf")

# Your own asset trees — viewer HTML/JS/CSS, dashboard assets, themes
datas += [
    ("backend/app/export/viewer_assets",  "backend/app/export/viewer_assets"),
    ("backend/app/reports/dashboard_assets", "backend/app/reports/dashboard_assets"),
    ("gui/app/views/widgets",             "gui/app/views/widgets"),
    ("gui/app/resources",                 "gui/app/resources"),
    ("shared",                            "shared"),
    ("logo.png",                          "."),
]

# ── Hidden imports PyInstaller misses ───────────────────────────────────────
hidden_imports = [
    # PySide6 WebEngine (the big one — must be explicit)
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
    "PySide6.QtNetwork",
    "PySide6.QtPrintSupport",

    # pycryptodome can live under Crypto or Cryptodome depending on install method
    "Crypto",
    "Crypto.Cipher",
    "Crypto.Cipher.AES",
    "Cryptodome",
    "Cryptodome.Cipher",
    "Cryptodome.Cipher.AES",

    # protobuf runtime
    "google.protobuf",
    "google.protobuf.descriptor",
    "google.protobuf.descriptor_pool",
    "google.protobuf.reflection",
    "google.protobuf.message_factory",

    # openpyxl
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.utils",

    # Pillow plugins for AVIF/WebP (Pillow 12+)
    "PIL._imaging",
    "PIL.ImageFile",
    "PIL.Image",

    # qt-material internal
    "qt_material",

    # backend modules that may be dynamically imported
    "backend.app.ingestion.orchestrator",
    "backend.app.ingestion.message_ingester",
    "backend.app.ingestion.media_ingester",
    "backend.app.ingestion.call_ingester",
    "backend.app.ingestion.revoke_ingester",
    "backend.app.ingestion.edit_ingester",
    "backend.app.ingestion.contact_resolver",
    "backend.app.ingestion.orphaned_media_ingester",
    "backend.app.ingestion.keyid_classifier",
    "backend.app.reports.group_report",
    "backend.app.reports.contact_report",
    "backend.app.reports.media_report",
    "backend.app.export.viewer_bundle_exporter",
    "backend.app.db.schema",
    "shared.system_event_formatter",
    "shared.forensic_provenance",
]

# Add all backend submodules dynamically (catches anything you missed above)
hidden_imports += collect_submodules("backend")
hidden_imports += collect_submodules("gui")
hidden_imports += collect_submodules("shared")

# ── Modules to EXCLUDE (saves 200–400 MB from final size) ───────────────────
excludes = [
    # Qt modules your app genuinely never uses
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets",
    # Python stdlib bloat
    "tkinter",
    "unittest",
    "email",
    "xml",
    "pydoc",
    # Other Qt bindings (PyInstaller 6+ aborts if two exist, be safe)
    "PyQt5",
    "PyQt6",
    "PySide2",
]

a = Analysis(
    ["wainsight.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    exclude_binaries=True,      # REQUIRED for onedir (QWebEngine needs sibling EXEs)
    name="WAInsight",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                   # UPX compress binaries if UPX is on PATH
    console=False,              # no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="logo.ico",            # convert logo.png → logo.ico first (see below)
    version="version_info.txt", # optional: Windows file version metadata
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # Do NOT UPX these — they're signed DLLs and UPX breaks their signatures
        "vcruntime140.dll",
        "python311.dll",
        "Qt6WebEngineCore.dll",
        "QtWebEngineProcess.exe",
    ],
    name="WAInsight",
)