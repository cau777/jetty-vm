// PROTOTYPE — Variant B "Board": no sidebar or table. VMs are cards in a flowing grid;
// creation is a centered modal that becomes a big progress view, then agent tiles.
import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts
import QtQuick.Dialogs

Item {
    id: root
    property string folder: backend.homeDir
    property string handoffMessage: ""

    function openCreate() { modal.open() }
    function closeCreate() { modal.close() }

    FolderDialog {
        id: folderDialog
        currentFolder: "file://" + root.folder
        onAccepted: root.folder = backend.localPath(selectedFolder)
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 32
        spacing: 22

        RowLayout {
            Layout.fillWidth: true
            Label { text: "Your agent machines"; font.pixelSize: 28; font.bold: true }
            Rectangle {
                radius: 10; height: 22; width: statusText.implicitWidth + 18
                color: backend.proxyState === "running" ? "#16332b" : "#3a2a12"
                Label { id: statusText; anchors.centerIn: parent; text: "proxy " + backend.proxyState; font.pixelSize: 11; color: backend.proxyState === "running" ? "#72e0bd" : "#ffc56e" }
            }
            Item { Layout.fillWidth: true }
            RoundButton {
                objectName: "proto_open_create"
                text: "+  New VM"
                highlighted: true
                padding: 18
                font.pixelSize: 15
                onClicked: modal.open()
            }
        }

        GridView {
            id: grid
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            cellWidth: Math.max(280, width / Math.max(1, Math.floor(width / 300)))
            cellHeight: 190
            model: backend.vms
            delegate: Item {
                id: card
                readonly property string vmName: modelData.name
                width: grid.cellWidth
                height: grid.cellHeight
                Pane {
                    anchors.fill: parent
                    anchors.margins: 8
                    Material.elevation: 3
                    Material.background: modelData.fresh ? "#132a24" : "#141a24"
                    padding: 18
                    Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.margins: -18; width: 4; color: modelData.status === "Running" ? "#72e0bd" : "#ffc56e" }
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 6
                        Label { text: modelData.name; font.pixelSize: 17; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                        Label { text: modelData.status + "  ·  " + modelData.ip_address; color: "#8793a5"; font.family: "monospace" }
                        Label { text: modelData.fresh ? "Not set up yet" : modelData.rules + " access rules"; color: modelData.fresh ? "#ffc56e" : "#8793a5" }
                        Item { Layout.fillHeight: true }
                        RowLayout {
                            Button {
                                text: "Prepare with…"
                                flat: !modelData.fresh
                                highlighted: modelData.fresh === true
                                onClicked: agentMenu.popup()
                                Menu {
                                    id: agentMenu
                                    Repeater {
                                        model: backend.agents
                                        MenuItem { text: modelData.label; onTriggered: root.handoffMessage = backend.handOff(card.vmName, modelData.key, root.folder) }
                                    }
                                }
                            }
                            Item { Layout.fillWidth: true }
                            ToolButton { text: modelData.status === "Running" ? "■" : "▶"; enabled: false }
                        }
                    }
                }
            }
        }
        Label { text: root.handoffMessage; visible: text !== ""; color: "#72e0bd"; Layout.bottomMargin: 48 }
    }

    Popup {
        id: modal
        modal: true
        anchors.centerIn: parent
        width: 620
        height: 440
        padding: 28
        Material.background: "#111720"
        Material.elevation: 12
        Overlay.modal: Rectangle { color: "#b3000000" }
        onClosed: if (backend.job.state !== "running") backend.dismissJob()

        StackLayout {
            anchors.fill: parent
            currentIndex: ({ "idle": 0, "running": 1, "failed": 1, "done": 2 })[backend.job.state] || 0

            // Form: one big question per row
            ColumnLayout {
                spacing: 14
                Label { text: "Name your new machine"; font.pixelSize: 24; font.bold: true }
                TextField { id: fName; placeholderText: "e.g. my-app  (contains 'fail' to test errors)"; font.pixelSize: 18; Layout.fillWidth: true }
                Label { text: "Size"; color: "#8793a5"; Layout.topMargin: 10 }
                RowLayout {
                    id: sizes
                    property int choice: 1
                    Repeater {
                        model: [["Small", 2, "4GiB", "20GiB"], ["Default", 4, "8GiB", "40GiB"], ["Large", 8, "16GiB", "80GiB"]]
                        Button {
                            Layout.fillWidth: true
                            checkable: true
                            checked: sizes.choice === index
                            onClicked: sizes.choice = index
                            text: modelData[0] + "\n" + modelData[1] + " CPU · " + modelData[2]
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                RowLayout {
                    Item { Layout.fillWidth: true }
                    Button { text: "Cancel"; flat: true; onClicked: modal.close() }
                    Button {
                        text: "Create"; highlighted: true
                        onClicked: {
                            const s = [[2, "4GiB", "20GiB"], [4, "8GiB", "40GiB"], [8, "16GiB", "80GiB"]][sizes.choice]
                            backend.startCreate(fName.text || "demo-vm", s[0], s[1], s[2])
                        }
                    }
                }
            }

            // Progress: big headline + bar + collapsible log
            ColumnLayout {
                spacing: 12
                Label {
                    text: backend.job.state === "failed" ? "Something went wrong" : "Building " + (backend.job.name || "")
                    font.pixelSize: 24; font.bold: true
                    color: backend.job.state === "failed" ? "#ff7b7b" : "#edf2f7"
                }
                Label {
                    text: backend.job.steps ? backend.job.steps[backend.job.step].label + "…" : ""
                    font.pixelSize: 16; color: "#8793a5"
                    visible: backend.job.state === "running"
                }
                ProgressBar { value: backend.job.progress || 0; Layout.fillWidth: true; Layout.topMargin: 10 }
                Label {
                    text: "Step " + ((backend.job.step || 0) + 1) + " of " + (backend.job.steps ? backend.job.steps.length : 0) + "  ·  " + (backend.job.elapsed || 0) + " s"
                    color: "#8793a5"; font.pixelSize: 12
                }
                Label { visible: backend.job.state === "failed"; text: backend.job.error || ""; wrapMode: Text.Wrap; Layout.fillWidth: true; color: "#ff7b7b" }
                Switch { id: showLog; text: "Show details" }
                ScrollView {
                    visible: showLog.checked
                    Layout.fillWidth: true; Layout.fillHeight: true
                    TextArea {
                        readOnly: true; font.family: "monospace"; font.pixelSize: 11
                        text: (backend.job.log || []).join("\n")
                        background: Rectangle { color: "#080b10"; radius: 6 }
                    }
                }
                Item { Layout.fillHeight: !showLog.checked }
                RowLayout {
                    visible: backend.job.state === "failed"
                    Item { Layout.fillWidth: true }
                    Button { text: "Close"; flat: true; onClicked: modal.close() }
                    Button { text: "Try again"; highlighted: true; onClicked: backend.dismissJob() }
                }
            }

            // Done: agent tiles
            ColumnLayout {
                spacing: 12
                Label { text: "✓  " + (backend.job.name || "") + " is ready"; font.pixelSize: 24; font.bold: true; color: "#72e0bd" }
                Label { text: "It is not set up yet. Pick the agent that should prepare it for your project."; color: "#8793a5"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: root.folder; font.family: "monospace"; elide: Text.ElideMiddle; Layout.fillWidth: true }
                    Button { text: "Change folder…"; flat: true; onClicked: folderDialog.open() }
                }
                GridLayout {
                    columns: 4
                    Layout.fillWidth: true
                    columnSpacing: 10
                    Repeater {
                        model: backend.agents
                        Button {
                            Layout.fillWidth: true
                            Layout.preferredWidth: 1
                            Layout.preferredHeight: 110
                            Material.roundedScale: Material.SmallScale
                            onClicked: root.handoffMessage = backend.handOff(backend.job.name, modelData.key, root.folder)
                            contentItem: ColumnLayout {
                                Rectangle {
                                    Layout.alignment: Qt.AlignHCenter
                                    width: 44; height: 44; radius: 12; color: "#1f2b3a"
                                    Label { anchors.centerIn: parent; text: modelData.label.substring(0, 1); font.pixelSize: 22; font.bold: true; color: "#72e0bd" }
                                }
                                Label { text: modelData.label; Layout.alignment: Qt.AlignHCenter }
                            }
                        }
                    }
                }
                Label { text: root.handoffMessage; wrapMode: Text.Wrap; Layout.fillWidth: true; color: "#72e0bd"; font.pixelSize: 12 }
                Item { Layout.fillHeight: true }
                RowLayout { Item { Layout.fillWidth: true } Button { text: "Done"; onClicked: modal.close() } }
            }
        }
    }
}
