import QtQuick
import QtQuick.Controls

Button {
    id: root
    property bool destructive: false
    implicitHeight: 36
    contentItem: Text {
        text: root.text
        font: root.font
        color: !root.enabled ? "#667386" : root.highlighted ? "#081411" : root.destructive ? "#ff9a9a" : "#edf2f7"
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        radius: 6
        color: !root.enabled ? "#171d26"
              : root.highlighted ? (root.down ? "#55b89f" : "#72e0bd")
              : root.flat ? (root.hovered ? "#202a37" : "transparent")
              : (root.down ? "#263548" : root.hovered ? "#253243" : "#1b2633")
        border.width: root.destructive ? 1 : 0
        border.color: root.destructive ? "#a64f59" : "transparent"
    }
}
