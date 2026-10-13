import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var backend
    required property string vmName
    spacing: 8

    Label {
        Layout.fillWidth: true
        text: "The agent opens in a temporary folder and asks what you want to do with this VM."
        color: "#8793a5"
        wrapMode: Text.Wrap
        font.pixelSize: 11
    }
    GridLayout {
        columns: 2
        Layout.fillWidth: true
        Repeater {
            model: root.backend.agents
            JettyButton {
                required property var modelData
                Layout.fillWidth: true
                text: modelData.label
                enabled: !root.backend.handoffMessage.startsWith("Opening ")
                onClicked: root.backend.handoff(root.vmName, modelData.key, "")
            }
        }
    }
    Label {
        visible: root.backend.handoffMessage !== ""
        Layout.fillWidth: true
        text: root.backend.handoffMessage
        color: "#72e0bd"
        wrapMode: Text.Wrap
        font.pixelSize: 11
    }
}
