# PyInstaller onedir build. The AppImage wraps this directory, keeping the
# interpreter, mitmproxy and the native Qt Quick UI available to both modes.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

spec_dir = Path(SPECPATH).resolve()
project_dir = spec_dir.parent
repo = project_dir.parent
datas = []
for include in (
    "qml/**",
    "resources/**",
    "migrations/**",
    "requests_migrations/**",
):
    datas += collect_data_files("orca_proxy", includes=[include])

for source, target in (
    (repo / "orca-proxy" / "deploy" / "gateway", "orca_proxy/deploy/gateway"),
    (repo / "orca-ssh-setup", "orca_ssh_setup"),
):
    for path in source.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            datas.append((str(path), str(Path(target) / path.relative_to(source).parent)))

datas += [
    (str(repo / "VERSION"), "orca_proxy"),
    (str(repo / "gh-rest" / "gh-rest.py"), "orca_proxy"),
    (str(spec_dir / "THIRD_PARTY_NOTICES.md"), "orca_proxy"),
]

a = Analysis(
    [str(project_dir / "src" / "jetty_entry.py")],
    pathex=[str(project_dir / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtQuickControls2",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtPdf", "PySide6.QtCharts",
        "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtQuick3D", "PySide6.QtGraphs",
        "PySide6.QtDataVisualization",
    ],
    noarchive=False,
    optimize=0,
)

DROP = re.compile(
    r"WebEngine|WebChannel|WebView|WebSockets|Qt63D|Qt3D|Quick3D|Charts|DataVisualization|Graphs|"
    r"Qt6Pdf|QtPdf|Multimedia|SpatialAudio|TextToSpeech|Sensors|Location|Positioning|"
    r"VirtualKeyboard|Scxml|StateMachine|RemoteObjects|Bluetooth|Nfc|SerialPort|SerialBus|"
    r"Designer|Help|Lottie|QuickTimeline|HttpServer|Svg/Widgets|UiTools|"
    r"Controls/(Fusion|Imagine|Universal|FluentWinUI3|iOS|macOS|Windows)|"
    r"QuickControls2(Fusion|Imagine|Universal|FluentWinUI3|IOS|MacOS|Windows)",
    re.IGNORECASE,
)
a.binaries = [binary for binary in a.binaries if not DROP.search(binary[0]) and not DROP.search(binary[1])]
a.datas = [data for data in a.datas if not DROP.search(data[0]) and not DROP.search(data[1])]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jetty",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="jetty",
)
