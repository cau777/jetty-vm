// PROTOTYPE — Variant A "Console": sidebar + table + inspector; create in a side sheet
// with a vertical stepper, then an agent handoff panel in the same sheet.
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs

Item {
    id: root
    property string selected: backend.vms.length ? backend.vms[0].name : ""
    property string handoffMessage: ""
    property string folder: backend.homeDir
    readonly property var selectedVm: backend.vms.find(v => v.name === selected) || null

    function openCreate() { sheet.open() }
    function closeCreate() { sheet.close() }

    FolderDialog {
        id: folderDialog
        currentFolder: "file://" + root.folder
        onAccepted: root.folder = backend.localPath(selectedFolder)
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        // Sidebar
        Rectangle {
            Layout.fillHeight: true
            Layout.preferredWidth: 210
            color: "#10151d"
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 14
                spacing: 4
                Label { text: "Jetty"; font.pixelSize: 18; font.bold: true; Layout.bottomMargin: 18; Layout.leftMargin: 8 }
                Repeater {
                    model: ["Logs", "Rules", "Credentials", "VMs"]
                    ItemDelegate {
                        Layout.fillWidth: true
                        text: modelData
                        highlighted: modelData === "VMs"
                    }
                }
                Item { Layout.fillHeight: true }
                Label {
                    text: (backend.live ? "● live daemon" : "● sample data") + " · proxy " + backend.proxyState
                    color: backend.live ? "#72e0bd" : "#ffc56e"; font.pixelSize: 11; Layout.leftMargin: 8
                }
            }
        }

        // Main table
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.margins: 26
            spacing: 16
            RowLayout {
                ColumnLayout {
                    spacing: 2
                    Label { text: "INVENTORY"; color: "#72e0bd"; font.pixelSize: 11; font.bold: true; font.letterSpacing: 1.5 }
                    Label { text: "Virtual machines  " + backend.vms.length; font.pixelSize: 22; font.bold: true }
                    Label { text: "Create and manage isolated LXD agents."; color: "#8793a5" }
                }
                Item { Layout.fillWidth: true }
                Button { objectName: "proto_open_create"; text: "New VM"; highlighted: true; onClicked: sheet.open() }
            }
            Pane {
                Layout.fillWidth: true
                Layout.fillHeight: true
                padding: 0
                Material.background: "#141a24"
                Material.elevation: 1
                ColumnLayout {
                    anchors.fill: parent
                    spacing: 0
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.margins: 12
                        Repeater {
                            model: [["NAME", 3], ["STATUS", 1.4], ["IP ADDRESS", 1.6], ["RULES", 0.8]]
                            Label { text: modelData[0]; color: "#8793a5"; font.pixelSize: 10; font.bold: true; Layout.fillWidth: true; Layout.preferredWidth: modelData[1] * 100 }
                        }
                    }
                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        model: backend.vms
                        delegate: ItemDelegate {
                            width: ListView.view.width
                            highlighted: modelData.name === root.selected
                            onClicked: root.selected = modelData.name
                            contentItem: RowLayout {
                                Label { text: modelData.name; font.bold: true; Layout.fillWidth: true; Layout.preferredWidth: 300; elide: Text.ElideRight }
                                RowLayout {
                                    Layout.fillWidth: true; Layout.preferredWidth: 140
                                    Rectangle { width: 8; height: 8; radius: 4; color: modelData.status === "Running" ? "#72e0bd" : "#ffc56e" }
                                    Label { text: modelData.status; font.pixelSize: 12 }
                                }
                                Label { text: modelData.ip_address; font.family: "monospace"; Layout.fillWidth: true; Layout.preferredWidth: 160 }
                                Label { text: modelData.rules; Layout.fillWidth: true; Layout.preferredWidth: 80 }
                            }
                        }
                    }
                }
            }
        }

        // Inspector
        Rectangle {
            Layout.fillHeight: true
            Layout.preferredWidth: 330
            color: "#10151d"
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 20
                spacing: 12
                visible: root.selectedVm !== null
                Label { text: "VM"; color: "#72e0bd"; font.pixelSize: 11; font.bold: true }
                Label { text: root.selectedVm ? root.selectedVm.name : ""; font.pixelSize: 20; font.bold: true; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                GridLayout {
                    columns: 2; columnSpacing: 14; rowSpacing: 6
                    Label { text: "Status"; color: "#8793a5" } Label { text: root.selectedVm ? root.selectedVm.status : "" }
                    Label { text: "IP address"; color: "#8793a5" } Label { text: root.selectedVm ? root.selectedVm.ip_address : ""; font.family: "monospace" }
                    Label { text: "Rules"; color: "#8793a5" } Label { text: root.selectedVm ? root.selectedVm.rules : "" }
                }
                RowLayout { Button { text: "Stop"; enabled: false } Button { text: "Restart"; enabled: false } }
                MenuSeparator { Layout.fillWidth: true }
                Label { text: "PREPARE WITH AN AGENT"; color: "#72e0bd"; font.pixelSize: 11; font.bold: true }
                AgentPicker { Layout.fillWidth: true; vm: root.selected }
                Item { Layout.fillHeight: true }
            }
        }
    }

    // Create sheet
    Drawer {
        id: sheet
        edge: Qt.RightEdge
        width: Math.min(520, root.width)
        height: root.height
        Material.background: "#111720"
        Overlay.modal: Rectangle { color: "#b3000000" }
        onClosed: if (backend.job.state !== "running") backend.dismissJob()

        ScrollView {
            anchors.fill: parent
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 14
                anchors.margins: 24
                Item { height: 10 }
                Label { text: "NEW VM"; color: "#72e0bd"; font.pixelSize: 11; font.bold: true; Layout.leftMargin: 24 }
                Label {
                    Layout.leftMargin: 24
                    font.pixelSize: 22; font.bold: true
                    text: backend.job.state === "idle" ? "Create an agent VM" : backend.job.name
                }

                // Form
                ColumnLayout {
                    visible: backend.job.state === "idle"
                    Layout.fillWidth: true; Layout.leftMargin: 24; Layout.rightMargin: 24
                    spacing: 6
                    TextField { id: fName; placeholderText: "Name (try one containing 'fail')"; Layout.fillWidth: true }
                    RowLayout {
                        SpinBox { id: fCpus; from: 1; to: 64; value: 4; editable: true }
                        Label { text: "CPUs"; color: "#8793a5" }
                    }
                    TextField { id: fMem; text: "8GiB"; placeholderText: "Memory"; Layout.fillWidth: true }
                    TextField { id: fDisk; text: "40GiB"; placeholderText: "Root disk"; Layout.fillWidth: true }
                    RowLayout {
                        Layout.topMargin: 10
                        Item { Layout.fillWidth: true }
                        Button { text: "Cancel"; flat: true; onClicked: sheet.close() }
                        Button { text: "Create VM"; highlighted: true; onClicked: backend.startCreate(fName.text || "demo-vm", fCpus.value, fMem.text, fDisk.text) }
                    }
                }

                // Stepper
                ColumnLayout {
                    visible: backend.job.state === "running" || backend.job.state === "failed"
                    Layout.fillWidth: true; Layout.leftMargin: 24; Layout.rightMargin: 24
                    spacing: 0
                    Repeater {
                        model: backend.job.steps || []
                        RowLayout {
                            spacing: 12
                            Layout.preferredHeight: 34
                            Item {
                                width: 20; height: 20
                                BusyIndicator { anchors.fill: parent; padding: 0; running: modelData.state === "running"; visible: running }
                                Label {
                                    anchors.centerIn: parent
                                    visible: modelData.state !== "running"
                                    text: modelData.state === "done" ? "✓" : modelData.state === "failed" ? "✕" : "○"
                                    color: modelData.state === "done" ? "#72e0bd" : modelData.state === "failed" ? "#ff7b7b" : "#556070"
                                    font.bold: true
                                }
                            }
                            Label {
                                text: modelData.label
                                color: modelData.state === "pending" ? "#556070" : modelData.state === "failed" ? "#ff7b7b" : "#edf2f7"
                                font.bold: modelData.state === "running"
                            }
                        }
                    }
                    Label { text: "Elapsed " + (backend.job.elapsed || 0) + " s"; color: "#8793a5"; font.pixelSize: 11; Layout.topMargin: 8 }
                    Label {
                        visible: backend.job.state === "failed"
                        text: backend.job.error || ""
                        color: "#ff7b7b"; wrapMode: Text.Wrap; Layout.fillWidth: true; Layout.topMargin: 8
                    }
                    RowLayout {
                        visible: backend.job.state === "failed"
                        Button { text: "Close"; onClicked: sheet.close() }
                        Button { text: "Try again"; highlighted: true; onClicked: backend.dismissJob() }
                    }
                }

                // Done: handoff
                ColumnLayout {
                    visible: backend.job.state === "done"
                    Layout.fillWidth: true; Layout.leftMargin: 24; Layout.rightMargin: 24
                    spacing: 12
                    Pane {
                        Layout.fillWidth: true
                        Material.background: "#16332b"
                        Label { text: "VM created successfully at " + (backend.job.ip || ""); color: "#72e0bd" }
                    }
                    Label {
                        Layout.fillWidth: true; wrapMode: Text.Wrap
                        text: "<b>It is not set up yet.</b> Use your agent to prepare it: it inspects your project, installs the toolchain and harnesses, and adds the access rules it needs."
                        textFormat: Text.RichText
                    }
                    AgentPicker { Layout.fillWidth: true; vm: backend.job.name || "" }
                    RowLayout { Item { Layout.fillWidth: true } Button { text: "Done"; onClicked: sheet.close() } }
                }
            }
        }
    }
}
