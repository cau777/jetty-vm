import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Dialogs
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var backend
    required property string vmName
    spacing: 8

    FolderDialog {
        id: folderDialog
        currentFolder: "file://" + encodeURI(root.backend.projectDir || root.backend.homeDir)
        onAccepted: root.backend.setProjectDir(selectedFolder.toLocalFile())
    }

    Label { text: "Project folder"; color: "#8793a5"; font.pixelSize: 11 }
    RowLayout {
        Layout.fillWidth: true
        TextField {
            Layout.fillWidth: true
            text: root.backend.projectDir
            placeholderText: "Home folder if empty"
            font.family: "monospace"
            onTextEdited: root.backend.setProjectDir(text)
        }
        JettyButton { text: "Browse…"; flat: true; onClicked: folderDialog.open() }
    }
    Label {
        Layout.fillWidth: true
        text: "The agent starts here, reads the project and prepares this VM."
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
                onClicked: root.backend.handoff(root.vmName, modelData.key, root.backend.projectDir)
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
