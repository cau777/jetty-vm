# PyInstaller onedir build. The AppImage wraps this directory, keeping the
# interpreter, mitmproxy, Qt and QtWebEngineProcess available to both modes.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

spec_dir = Path(SPECPATH).resolve()
project_dir = spec_dir.parent
repo = project_dir.parent
datas = []
for include in (
    "static/**",
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
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
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
