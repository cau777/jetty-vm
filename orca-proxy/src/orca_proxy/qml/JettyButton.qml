import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: root
    property bool destructive: false
    property string iconText: ""
    implicitHeight: 36
    font.pixelSize: 13
    leftPadding: iconText === "" ? 12 : 10
    rightPadding: iconText === "" ? 12 : 10
    topPadding: 0
    bottomPadding: 0
    clip: true
    contentItem: RowLayout {
        spacing: root.iconText === "" ? 0 : 7
        clip: true
        Label {
            visible: root.iconText !== ""
            Layout.fillHeight: true
            Layout.alignment: Qt.AlignVCenter
            text: root.iconText
            color: !root.enabled ? "#766f62" : root.highlighted ? "#201a10" : root.destructive ? "#ff9a9a" : "#f1ece3"
            font.pixelSize: 14
            font.bold: true
            verticalAlignment: Text.AlignVCenter
        }
        Text {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignVCenter
            text: root.text
            font: root.font
            color: !root.enabled ? "#766f62" : root.highlighted ? "#201a10" : root.destructive ? "#ff9a9a" : "#f1ece3"
            horizontalAlignment: root.iconText === "" ? Text.AlignHCenter : Text.AlignLeft
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            wrapMode: Text.NoWrap
            maximumLineCount: 1
            clip: true
        }
    }
    background: Rectangle {
        radius: 6
        color: !root.enabled ? "#24211b"
              : root.highlighted ? (root.down ? "#c9a367" : "#dfba80")
              : root.flat ? (root.hovered ? "#332e25" : "transparent")
              : (root.down ? "#3a3022" : root.hovered ? "#342e23" : "#29261f")
        border.width: root.destructive || !root.highlighted ? 1 : 0
        border.color: root.destructive ? "#a64f59" : root.highlighted ? "transparent" : "#3b362c"
    }
}
