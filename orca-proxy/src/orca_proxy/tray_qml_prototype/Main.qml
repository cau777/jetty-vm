// PROTOTYPE — throwaway. Hosts the variants and the prototype switcher bar.
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ApplicationWindow {
    id: win
    width: backend.variant === "C" ? 440 : 1240
    height: backend.variant === "C" ? 760 : 800
    visible: true
    title: "Jetty (QML prototype " + backend.variant + ")"
    color: "#0c1017"
    Material.theme: Material.Dark
    Material.accent: "#72e0bd"
    Material.primary: "#141a24"
    Material.background: "#0c1017"

    readonly property var variantNames: ({ "A": "Console", "B": "Board", "C": "Tray popover" })

    function openCreate() { if (loader.item) loader.item.openCreate() }
    function closeCreate() { if (loader.item) loader.item.closeCreate() }

    Loader {
        id: loader
        anchors.fill: parent
        source: "Variant" + backend.variant + ".qml"
    }

    // Left/Right cycle variants, except while typing in a text field.
    readonly property bool typing: activeFocusItem !== null && activeFocusItem.hasOwnProperty("cursorPosition")
    Shortcut { sequence: "Left"; enabled: !win.typing; onActivated: backend.cycle(-1) }
    Shortcut { sequence: "Right"; enabled: !win.typing; onActivated: backend.cycle(1) }

    Rectangle {
        z: 1000
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 14
        height: 38
        width: switcherRow.implicitWidth + 12
        radius: height / 2
        color: "#fafafa"
        border.color: "#000"
        RowLayout {
            id: switcherRow
            anchors.centerIn: parent
            spacing: 2
            Button { flat: true; text: "‹"; font.pixelSize: 20; Material.foreground: "#111"; onClicked: backend.cycle(-1) }
            Label {
                text: "PROTOTYPE  " + backend.variant + " — " + win.variantNames[backend.variant]
                color: "#111"; font.bold: true; font.pixelSize: 12
            }
            Button { flat: true; text: "›"; font.pixelSize: 20; Material.foreground: "#111"; onClicked: backend.cycle(1) }
        }
    }
}
