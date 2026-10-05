# Third party notices

Jetty bundles the following third party components. This directory also
contains the LGPL-3.0, GPL-2.0 and GPL-3.0 license texts used by the Qt for
Python package's license alternatives.

## Qt for Python and Qt

This Jetty distribution uses the LGPL-3.0-only terms offered by PySide6 and
ships Qt libraries as separate shared libraries. To inspect or modify the
bundle, extract the AppImage with `./jetty-x86_64.AppImage --appimage-extract`,
edit the shared libraries under `squashfs-root/usr/lib/jetty`, and run
`squashfs-root/AppRun`.

The corresponding Qt 6.11.2 source is available from Qt's official source
archive at
<https://download.qt.io/official_releases/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz>.
PySide6 6.11.2 source is available from the official repository at
<https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=6.11.2>. Qt's open
source license information is at <https://www.qt.io/development/open-source-lgpl-obligations>.

## PyInstaller

PyInstaller is distributed under the GNU General Public License (GPL), with
an exception permitting distribution of applications built with it under
other licenses. Its source and license are available at
<https://github.com/pyinstaller/pyinstaller>.

## mitmproxy

mitmproxy and mitmproxy_rs are distributed under their upstream licenses.
Their source and license files are available at
<https://github.com/mitmproxy/mitmproxy>.
