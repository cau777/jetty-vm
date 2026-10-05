# PROTOTYPE — measures the bundle a QML tray would ship: the full daemon and CLI
# (mitmproxy and friends) plus Qt Quick, with QtWebEngine and unused Qt modules
# dropped. Build from orca-proxy/:
#   uv run pyinstaller --noconfirm --distpath build/proto-dist --workpath build/proto-work \
#     src/orca_proxy/tray_qml_prototype/size.spec
import re
from pathlib import Path

proto = Path(SPECPATH).resolve()
src = proto.parents[1]

DROP = re.compile(
    r"WebEngine|WebChannel|WebView|WebSockets|Qt63D|Qt3D|Quick3D|Charts|DataVisualization|Graphs|"
    r"Qt6Pdf|QtPdf|Multimedia|SpatialAudio|TextToSpeech|Sensors|Location|Positioning|"
    r"VirtualKeyboard|Scxml|StateMachine|RemoteObjects|Bluetooth|Nfc|SerialPort|SerialBus|"
    r"Designer|Help|Lottie|QuickTimeline|HttpServer|Svg/Widgets|UiTools|"
    # Qt Quick Controls styles other than Basic and Material
    r"Controls/(Fusion|Imagine|Universal|FluentWinUI3|iOS|macOS|Windows)|QuickControls2(Fusion|Imagine|Universal|FluentWinUI3|IOS|MacOS|Windows)",
    re.IGNORECASE,
)

a = Analysis(
    [str(proto / "size_entry.py")],
    pathex=[str(src)],
    datas=[(str(p), "orca_proxy/tray_qml_prototype") for p in proto.glob("*.qml")],
    hiddenimports=["PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuickControls2"],
    excludes=[
        "orca_proxy.tray",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtPdf", "PySide6.QtCharts",
        "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtQuick3D", "PySide6.QtGraphs",
        "PySide6.QtDataVisualization",
    ],
)
a.binaries = [b for b in a.binaries if not DROP.search(b[0]) and not DROP.search(b[1])]
a.datas = [d for d in a.datas if not DROP.search(d[0]) and not DROP.search(d[1])]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="jetty", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="jetty")
