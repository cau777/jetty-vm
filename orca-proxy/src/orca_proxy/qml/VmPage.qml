import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var backend
    signal confirmationRequested(string kind, string name, string action)
    property var vm: backend.selection
    spacing: 18

    Label {
        visible: !root.vm.name
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.alignment: Qt.AlignCenter
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        text: "Select a VM in the sidebar, or create a new one."
        color: "#afa698"
    }

    RowLayout {
        visible: !!root.vm.name
        Layout.fillWidth: true
        spacing: 12
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 3
            Label { text: "VIRTUAL MACHINES"; color: "#dfba80"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.3 }
            Label { text: root.vm.name || ""; font.pixelSize: 25; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
        }
        Rectangle {
            radius: 6
            color: root.vm.status === "Running" ? "#332d20" : "#29261f"
            border.color: "#3b362c"
            implicitWidth: statusLabel.implicitWidth + 18
            implicitHeight: 28
            Label { id: statusLabel; anchors.centerIn: parent; text: root.vm.status || "Unknown"; color: "#dfba80"; font.pixelSize: 11; font.bold: true }
        }
        Item { Layout.fillWidth: true; Layout.minimumWidth: 4 }
        JettyButton {
            text: root.vm.status === "Running" ? "Stop" : "Start"
            iconText: root.vm.status === "Running" ? "■" : "▶"
            onClicked: root.confirmationRequested("vm", root.vm.name, root.vm.status === "Running" ? "stop" : "start")
        }
        JettyButton { text: "Restart"; iconText: "↻"; flat: true; onClicked: root.confirmationRequested("vm", root.vm.name, "restart") }
        JettyButton { text: "Open files"; iconText: "↗"; highlighted: true; enabled: root.vm.status === "Running"; onClicked: root.backend.openFiles(root.vm.name) }
        JettyButton { text: "Delete"; iconText: "⌫"; destructive: true; flat: true; onClicked: root.confirmationRequested("vm", root.vm.name, "delete") }
    }

    Pane {
        visible: !!root.vm.name
        Layout.fillWidth: true
        padding: 0
        background: Rectangle { color: "#201e1a"; radius: 10; border.color: "#3b362c" }
        RowLayout {
            anchors.fill: parent
            spacing: 0
            Repeater {
                model: [
                    {glyph: "⌖", label: "IP ADDRESS", value: root.vm.ip_address || "—"},
                    {glyph: "◇", label: "VM RULES", value: root.vm.rules === undefined ? "—" : String(root.vm.rules)},
                    {glyph: "↗", label: "PORT LINKS", value: String((root.backend.portForwards || []).filter(port => port.vm_name === root.vm.name).length + (root.backend.reverseForwards || []).filter(port => port.vm_name === root.vm.name).length)},
                    {glyph: "▣", label: "CPU", value: root.vm.cpus ? root.vm.cpus + (String(root.vm.cpus) === "1" ? " core" : " cores") : "—"},
                    {glyph: "▤", label: "RAM", value: root.vm.memory || "—"},
                    {glyph: "◫", label: "DISK", value: root.vm.disk || "—"}
                ]
                delegate: Item {
                    id: cell
                    required property var modelData
                    required property int index
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.preferredHeight: 76
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 18
                        anchors.rightMargin: 18
                        spacing: 10
                        Label { text: cell.modelData.glyph; color: "#dfba80"; font.pixelSize: 17; Layout.alignment: Qt.AlignTop; Layout.topMargin: 16 }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            spacing: 4
                            Label { text: cell.modelData.label; color: "#afa698"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 1 }
                            Label { text: cell.modelData.value; color: "#f1ece3"; font.pixelSize: 13; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true; font.family: cell.modelData.label === "IP ADDRESS" ? "monospace" : "sans-serif" }
                        }
                    }
                    Rectangle { visible: cell.index < 5; anchors.right: parent.right; anchors.top: parent.top; anchors.bottom: parent.bottom; width: 1; color: "#3b362c" }
                }
            }
        }
    }

    Label {
        visible: root.backend.message !== "" && !!root.vm.name
        Layout.fillWidth: true
        text: root.backend.message
        color: root.backend.messageTone === "danger" ? "#ff7b7b" : root.backend.messageTone === "success" ? "#dfba80" : "#96bce8"
        wrapMode: Text.Wrap
    }

    RowLayout {
        visible: !!root.vm.name
        Layout.fillWidth: true
        Layout.fillHeight: true
        spacing: 18

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumWidth: 430
            Layout.preferredWidth: 600

            Inspector {
                Layout.fillWidth: true
                Layout.fillHeight: true
                backend: root.backend
                onConfirmationRequested: (kind, name, action) => root.confirmationRequested(kind, name, action)
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumWidth: 320
            Layout.preferredWidth: 420
            spacing: 12

            Pane {
                Layout.fillWidth: true
                padding: 16
                background: Rectangle { color: "#201e1a"; radius: 10; border.color: "#3b362c" }
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10
                    Label { text: "AGENT SETUP"; color: "#dfba80"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.2 }
                    AgentHandoff { Layout.fillWidth: true; backend: root.backend; vmName: root.vm.name || "" }
                }
            }

            Pane {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 180
                padding: 18
                background: Rectangle { color: "#201e1a"; radius: 10; border.color: "#3b362c" }
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: "VM rules"; font.pixelSize: 18; font.bold: true }
                        JettyButton { text: "New rule"; iconText: "＋"; highlighted: true; onClicked: root.backend.openEditor("rules", "") }
                    }
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        contentWidth: availableWidth
                        ColumnLayout {
                            width: parent.width
                            spacing: 8
                            Repeater {
                                model: root.backend.vmRulesModel
                                delegate: Pane {
                                    required property var item
                                    Layout.fillWidth: true
                                    padding: 13
                                    background: Rectangle { color: "#29261f"; radius: 8; border.color: "#3b362c" }
                                    ColumnLayout {
                                        width: parent.width
                                        spacing: 6
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Label { Layout.fillWidth: true; text: item.name; font.bold: true; elide: Text.ElideRight }
                                            Label { text: "Priority " + item.priority; color: "#afa698"; font.pixelSize: 10 }
                                        }
                                        Label { Layout.fillWidth: true; text: item.hostname; color: "#96bce8"; wrapMode: Text.WrapAnywhere; font.family: "monospace"; font.pixelSize: 11 }
                                        Label { Layout.fillWidth: true; text: (item.action.type || "").replace(/_/g, " ") + (item.action.credential ? " · " + item.action.credential : "") + (item.action.path_prefix ? " · " + item.action.path_prefix : ""); color: "#c3bcae"; wrapMode: Text.Wrap; font.pixelSize: 11 }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            Item { Layout.fillWidth: true }
                                            JettyButton { text: "Edit"; iconText: "✎"; flat: true; onClicked: root.backend.openEditor("rules", item.name) }
                                            JettyButton { text: "Delete"; iconText: "⌫"; destructive: true; flat: true; onClicked: root.confirmationRequested("rules", item.name, "delete") }
                                        }
                                    }
                                }
                            }
                            Label { visible: root.backend.vmRulesModel.count === 0; Layout.fillWidth: true; Layout.margins: 12; text: "No VM rules yet. Global rules still apply."; color: "#afa698"; wrapMode: Text.Wrap }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#3b362c" }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: "Global rules apply after these."; color: "#afa698"; wrapMode: Text.Wrap; font.pixelSize: 11 }
                        JettyButton { text: "Global rules"; iconText: "◇"; flat: true; onClicked: root.backend.setView("rules") }
                    }
                }
            }
        }
    }
}
