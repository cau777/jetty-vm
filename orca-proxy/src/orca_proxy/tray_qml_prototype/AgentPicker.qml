// PROTOTYPE — Variant A's handoff block: native folder picker + agent buttons (stubbed).
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs

ColumnLayout {
    id: picker
    property string vm: ""
    property string folder: backend.homeDir
    property string message: ""
    spacing: 8

    FolderDialog {
        id: dialog
        currentFolder: "file://" + picker.folder
        onAccepted: picker.folder = backend.localPath(selectedFolder)
    }

    Label { text: "Project folder"; color: "#8793a5"; font.pixelSize: 11 }
    RowLayout {
        Layout.fillWidth: true
        TextField { text: picker.folder; onTextEdited: picker.folder = text; Layout.fillWidth: true; font.family: "monospace" }
        Button { text: "Browse…"; onClicked: dialog.open() }
    }
    GridLayout {
        columns: 2
        Layout.fillWidth: true
        Repeater {
            model: backend.agents
            Button {
                Layout.fillWidth: true
                text: modelData.label
                onClicked: picker.message = backend.handOff(picker.vm, modelData.key, picker.folder)
            }
        }
    }
    Label { text: picker.message; visible: text !== ""; color: "#72e0bd"; wrapMode: Text.Wrap; Layout.fillWidth: true; font.pixelSize: 12 }
}
