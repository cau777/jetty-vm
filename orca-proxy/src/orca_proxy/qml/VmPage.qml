import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

RowLayout {
    id: root
    required property var backend
    signal confirmationRequested(string kind, string name, string action)
    spacing: 0

    Label {
        visible: !root.backend.selection.name
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignCenter
        horizontalAlignment: Text.AlignHCenter
        text: "Select a VM in the sidebar, or create a new one."
        color: "#8793a5"
    }

    Inspector {
        visible: !!root.backend.selection.name
        Layout.fillHeight: true
        Layout.fillWidth: true
        Layout.preferredWidth: 480
        backend: root.backend
        onConfirmationRequested: (kind, name, action) => root.confirmationRequested(kind, name, action)
    }

    Pane {
        visible: !!root.backend.selection.name
        Layout.fillHeight: true
        Layout.fillWidth: true
        Layout.preferredWidth: 600
        padding: 26
        Material.background: "#0c1017"
        ColumnLayout {
            anchors.fill: parent
            spacing: 14
            RowLayout {
                Layout.fillWidth: true
                ColumnLayout {
                    Layout.fillWidth: true
                    Label { text: "VM RULES"; color: "#72e0bd"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.2 }
                    Label { text: "Rules for this VM"; font.pixelSize: 22; font.bold: true }
                }
                JettyButton { text: "+  New rule"; highlighted: true; onClicked: root.backend.openEditor("rules", "") }
            }
            Label { Layout.fillWidth: true; text: "Evaluated first, in priority order. Names and priorities belong to this VM."; color: "#8793a5"; wrapMode: Text.Wrap }
            Label {
                visible: root.backend.message !== ""
                Layout.fillWidth: true
                text: root.backend.message
                color: root.backend.messageTone === "danger" ? "#ff7b7b" : root.backend.messageTone === "success" ? "#72e0bd" : "#7eb7ff"
                wrapMode: Text.Wrap
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth
                ColumnLayout {
                    width: parent.width
                    spacing: 10
                    Repeater {
                        model: root.backend.vmRulesModel
                        delegate: Pane {
                            required property var item
                            Layout.fillWidth: true
                            padding: 16
                            Material.background: "#141a24"
                            ColumnLayout {
                                width: parent.width
                                spacing: 7
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { Layout.fillWidth: true; text: item.name; font.bold: true; elide: Text.ElideRight }
                                    Label { text: "Priority " + item.priority; color: "#8793a5"; font.pixelSize: 11 }
                                }
                                Label { Layout.fillWidth: true; text: item.hostname; color: "#7eb7ff"; wrapMode: Text.WrapAnywhere; font.family: "monospace" }
                                Label { Layout.fillWidth: true; text: (item.action.type || "").replace(/_/g, " ") + (item.action.credential ? " · " + item.action.credential : "") + (item.action.path_prefix ? " · " + item.action.path_prefix : ""); color: "#aab5c4"; wrapMode: Text.Wrap }
                                RowLayout {
                                    Item { Layout.fillWidth: true }
                                    JettyButton { text: "Edit"; flat: true; onClicked: root.backend.openEditor("rules", item.name) }
                                    JettyButton { text: "Delete"; destructive: true; onClicked: root.confirmationRequested("rules", item.name, "delete") }
                                }
                            }
                        }
                    }
                    Label { visible: root.backend.vmRulesModel.count === 0; Layout.fillWidth: true; text: "No VM rules yet. Global rules still apply."; color: "#8793a5"; wrapMode: Text.Wrap }
                }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: "#2b3544" }
            RowLayout {
                Layout.fillWidth: true
                Label { Layout.fillWidth: true; text: "Global rules apply after this VM’s rules."; color: "#8793a5"; wrapMode: Text.Wrap; font.pixelSize: 11 }
                JettyButton { text: "Global rules"; flat: true; onClicked: root.backend.setView("rules") }
            }
        }
    }
}
