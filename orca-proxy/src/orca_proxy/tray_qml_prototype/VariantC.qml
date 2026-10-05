// PROTOTYPE — Variant C "Tray popover": a narrow menu-bar style window. No dialogs or
// sheets: rows expand inline, the create form expands at the bottom, and a creating
// VM is a live row at the top of the list that turns into the handoff chips.
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs

Item {
    id: root
    property string expanded: ""
    property bool creating: false
    property string folder: backend.homeDir
    property string message: ""
    readonly property bool jobVisible: backend.job.state !== "idle"

    function openCreate() { creating = true }
    Connections {
        target: backend
        property string lastState: "idle"
        function onJobChanged() {
            if (backend.job.state === "running") root.creating = false
            // The pinned row changes height between states; keep it in view.
            if (backend.job.state !== lastState) lastState = backend.job.state
        }
    }
    function closeCreate() { creating = false; backend.dismissJob() }

    FolderDialog {
        id: folderDialog
        currentFolder: "file://" + root.folder
        onAccepted: root.folder = backend.localPath(selectedFolder)
    }

    component Chips: Flow {
        property string vm
        spacing: 6
        Repeater {
            model: backend.agents
            Button {
                text: modelData.label
                font.pixelSize: 12
                Material.background: "#1f2b3a"
                Material.roundedScale: Material.FullScale
                onClicked: root.message = backend.handOff(parent.vm, modelData.key, root.folder)
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Header with the proxy state, like a tray menu title
        Pane {
            Layout.fillWidth: true
            Material.background: "#141a24"
            padding: 16
            RowLayout {
                anchors.fill: parent
                Rectangle { width: 10; height: 10; radius: 5; color: backend.proxyState === "running" ? "#72e0bd" : "#ff7b7b" }
                ColumnLayout {
                    spacing: 0
                    Label { text: "Jetty"; font.bold: true; font.pixelSize: 16 }
                    Label { text: "Proxy " + backend.proxyState + (backend.live ? "" : " · sample data"); color: "#8793a5"; font.pixelSize: 11 }
                }
                Item { Layout.fillWidth: true }
                ToolButton { text: "⚙"; enabled: false }
            }
        }

        // A creating VM is a live row pinned above the others
        Pane {
            Layout.fillWidth: true
            visible: root.jobVisible
            padding: 14
            Material.background: backend.job.state === "failed" ? "#2d1717" : "#12201c"
            ColumnLayout {
                width: parent.width
                spacing: 6
                RowLayout {
                    Layout.fillWidth: true
                    BusyIndicator { running: backend.job.state === "running"; visible: running; implicitWidth: 28; implicitHeight: 28; padding: 2 }
                    Label { text: backend.job.state === "done" ? "✓" : backend.job.state === "failed" ? "✕" : ""; visible: text !== ""; color: backend.job.state === "done" ? "#72e0bd" : "#ff7b7b"; font.bold: true }
                    Label { text: backend.job.name || ""; font.bold: true; Layout.fillWidth: true; elide: Text.ElideRight }
                    Label { text: backend.job.state === "done" ? backend.job.ip : (backend.job.elapsed || 0) + " s"; color: "#8793a5"; font.family: "monospace"; font.pixelSize: 11 }
                }
                ProgressBar { visible: backend.job.state === "running"; value: backend.job.progress || 0; Layout.fillWidth: true }
                Label {
                    visible: backend.job.state === "running"
                    text: backend.job.steps ? backend.job.steps[backend.job.step].label : ""
                    color: "#8793a5"; font.pixelSize: 11
                }
                Label { visible: backend.job.state === "failed"; text: backend.job.error || ""; color: "#ff7b7b"; wrapMode: Text.Wrap; Layout.fillWidth: true; font.pixelSize: 11 }
                ColumnLayout {
                    visible: backend.job.state === "done"
                    Layout.fillWidth: true
                    spacing: 4
                    Label { text: "Not set up yet. Prepare it with:"; color: "#ffc56e"; font.pixelSize: 12 }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "in " + root.folder; color: "#8793a5"; font.pixelSize: 11; elide: Text.ElideMiddle; Layout.fillWidth: true }
                        Button { text: "Change"; flat: true; font.pixelSize: 11; onClicked: folderDialog.open() }
                    }
                    Chips { vm: backend.job.name || ""; Layout.fillWidth: true }
                }
                RowLayout {
                    visible: backend.job.state !== "running"
                    Item { Layout.fillWidth: true }
                    Button { text: "Dismiss"; flat: true; font.pixelSize: 11; onClicked: backend.dismissJob() }
                }
            }
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 1
            model: backend.vms

            delegate: Pane {
                id: row
                width: list.width
                padding: 0
                Material.background: "#10151d"
                readonly property bool open: root.expanded === modelData.name
                ColumnLayout {
                    width: parent.width
                    spacing: 0
                    ItemDelegate {
                        Layout.fillWidth: true
                        onClicked: root.expanded = row.open ? "" : modelData.name
                        contentItem: RowLayout {
                            Rectangle { width: 8; height: 8; radius: 4; color: modelData.status === "Running" ? "#72e0bd" : "#556070" }
                            Label { text: modelData.name; Layout.fillWidth: true; elide: Text.ElideRight }
                            Label { text: modelData.ip_address; color: "#8793a5"; font.family: "monospace"; font.pixelSize: 11 }
                            Label { text: row.open ? "▾" : "▸"; color: "#8793a5" }
                        }
                    }
                    ColumnLayout {
                        visible: row.open
                        Layout.fillWidth: true
                        Layout.leftMargin: 34; Layout.rightMargin: 14; Layout.bottomMargin: 12
                        spacing: 4
                        Label { text: modelData.status + " · " + modelData.rules + " access rules"; color: "#8793a5"; font.pixelSize: 12 }
                        RowLayout {
                            Button { text: modelData.status === "Running" ? "Stop" : "Start"; flat: true; enabled: false }
                            Button { text: "SSH"; flat: true; enabled: false }
                            Button { text: "Delete"; flat: true; enabled: false; Material.foreground: "#ff7b7b" }
                        }
                        Label { text: "Prepare with an agent"; color: "#8793a5"; font.pixelSize: 11 }
                        Chips { vm: modelData.name; Layout.fillWidth: true }
                    }
                }
            }
        }

        Label { text: root.message; visible: text !== ""; wrapMode: Text.Wrap; Layout.fillWidth: true; Layout.margins: 10; color: "#72e0bd"; font.pixelSize: 11 }

        // Inline create form at the bottom
        Pane {
            Layout.fillWidth: true
            Layout.bottomMargin: 56
            Material.background: "#141a24"
            padding: 12
            ColumnLayout {
                anchors.fill: parent
                ItemDelegate {
                    objectName: "proto_open_create"
                    Layout.fillWidth: true
                    visible: !root.creating
                    text: "+  New VM"
                    onClicked: root.creating = true
                }
                ColumnLayout {
                    visible: root.creating
                    Layout.fillWidth: true
                    RowLayout {
                        TextField { id: fName; placeholderText: "Name ('fail' tests errors)"; Layout.fillWidth: true }
                        ComboBox { id: fSize; model: ["Small", "Default", "Large"]; currentIndex: 1; Layout.preferredWidth: 130 }
                    }
                    RowLayout {
                        Item { Layout.fillWidth: true }
                        Button { text: "Cancel"; flat: true; onClicked: root.creating = false }
                        Button {
                            text: "Create"; highlighted: true
                            enabled: backend.job.state !== "running"
                            onClicked: {
                                const s = [[2, "4GiB", "20GiB"], [4, "8GiB", "40GiB"], [8, "16GiB", "80GiB"]][fSize.currentIndex]
                                backend.startCreate(fName.text || "demo-vm", s[0], s[1], s[2])
                                root.creating = false
                                list.positionViewAtBeginning()
                            }
                        }
                    }
                }
            }
        }
    }
}
