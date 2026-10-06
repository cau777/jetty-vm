import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ApplicationWindow {
    id: root
    property var controller: backend
    width: 1320
    height: 820
    minimumWidth: 980
    minimumHeight: 620
    visible: true
    title: "Jetty"
    color: "#0c1017"
    Material.theme: Material.Dark
    Material.accent: "#72e0bd"
    Material.primary: "#141a24"
    Material.background: "#0c1017"

    property var views: [
        {key: "logs", label: "Logs", glyph: "↗"},
        {key: "rules", label: "Rules", glyph: "◇"},
        {key: "credentials", label: "Credentials", glyph: "▣"},
        {key: "vms", label: "VMs", glyph: "▦"}
    ]

    function rowKey(row) { return backend.activeView === "logs" ? String(row.id) : String(row.name || "") }
    function rowPrimary(row) {
        if (backend.activeView === "logs") return row.destination_hostname || row.destination_ip || "Unknown destination"
        return row.name || ""
    }
    function rowSecondary(row) {
        if (backend.activeView === "logs") return row.vm_name + " · " + row.destination_port + " · " + String(row.started_at || "").replace("T", " ").slice(0, 19)
        if (backend.activeView === "rules") return row.hostname + " · " + (row.vm_selector && row.vm_selector.type === "all" ? "all VMs" : (row.vm_selector && row.vm_selector.vms || []).join(", "))
        return row.ip_address || "No IP address"
    }
    function rowStatus(row) {
        if (backend.activeView === "logs") return row.intercepted ? "intercepted" : (row.outcome || "unknown")
        if (backend.activeView === "rules") return row.action ? row.action.type.replace(/_/g, " ") : "unknown"
        if (backend.activeView === "credentials") return row.status || "empty"
        return row.status || "unknown"
    }
    function rowTone(row) {
        if (backend.activeView === "logs") return row.outcome === "block_rule" ? "#ff7b7b" : "#72e0bd"
        if (backend.activeView === "credentials") return row.status === "valid" ? "#72e0bd" : row.status === "error" ? "#ff7b7b" : "#ffc56e"
        if (backend.activeView === "vms") return row.status === "Running" ? "#72e0bd" : "#ffc56e"
        return row.action && row.action.type === "block" ? "#ff7b7b" : row.action && row.action.type === "allow_with_credential" ? "#7eb7ff" : "#72e0bd"
    }
    function showConfirmation(kind, name, action) {
        confirmation.kind = kind
        confirmation.name = name
        confirmation.action = action
        confirmation.open()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Top bar
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 58
            spacing: 12
            Label { text: "JETTY"; font.pixelSize: 15; font.bold: true; color: "#edf2f7"; Layout.leftMargin: 20 }
            Item { Layout.fillWidth: true }
            Rectangle { width: 8; height: 8; radius: 4; color: backend.proxyState === "running" ? "#72e0bd" : backend.proxyState === "failed" ? "#ff7b7b" : "#ffc56e" }
            Label { text: "proxy " + backend.proxyState; color: "#aab5c4"; font.pixelSize: 11 }
            Label { visible: backend.gatewayState !== "unknown"; text: "gateway " + backend.gatewayState; color: "#8793a5"; font.pixelSize: 11 }
            Label { visible: backend.tunnelState !== "unknown"; text: backend.tunnelState; color: "#8793a5"; font.pixelSize: 11; Layout.rightMargin: 22 }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Left navigation
            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 205
                color: "#10151d"
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 5
                    Label { text: "MANAGEMENT"; color: "#667386"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.2; Layout.leftMargin: 9; Layout.bottomMargin: 7 }
                    Repeater {
                        model: root.views
                        delegate: ItemDelegate {
                            required property var modelData
                            Layout.fillWidth: true
                            highlighted: backend.activeView === modelData.key
                            text: modelData.glyph + "    " + modelData.label
                            onClicked: backend.setView(modelData.key)
                            background: Rectangle { radius: 7; color: highlighted ? "#1b2935" : "transparent" }
                        }
                    }
                    Item { Layout.fillHeight: true }
                    Label { text: backend.ready ? "● daemon ready" : "● daemon unavailable"; color: backend.ready ? "#72e0bd" : "#ffc56e"; font.pixelSize: 11; Layout.leftMargin: 9 }
                    Label { text: "LXD " + (backend.lxdAvailable ? "available" : "unavailable"); color: "#8793a5"; font.pixelSize: 10; Layout.leftMargin: 9; Layout.bottomMargin: 5 }
                }
            }

            SetupPanel {
                visible: root.controller.needsSetup
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 28
                backend: root.controller
            }

            RowLayout {
                visible: !backend.needsSetup
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 0

                // Main table
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.margins: 26
                    spacing: 15

                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            spacing: 2
                            Label { text: "INVENTORY"; color: "#72e0bd"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.4 }
                            Label { text: backend.viewTitle + "  " + backend.currentModel.count; font.pixelSize: 22; font.bold: true }
                            Label { text: backend.viewSubtitle; color: "#8793a5"; font.pixelSize: 12 }
                        }
                        Item { Layout.fillWidth: true }
                        JettyButton { text: "Refresh"; flat: true; onClicked: backend.refresh() }
                        JettyButton {
                            visible: backend.activeView !== "logs"
                            text: backend.activeView === "vms" ? "+  New VM" : backend.activeView === "rules" ? "+  New rule" : "+  New credential"
                            highlighted: true
                            onClicked: backend.openEditor(backend.activeView, "")
                        }
                    }

                    Label {
                        visible: backend.message !== ""
                        Layout.fillWidth: true
                        text: backend.message
                        color: backend.messageTone === "danger" ? "#ff7b7b" : backend.messageTone === "success" ? "#72e0bd" : "#7eb7ff"
                        wrapMode: Text.Wrap
                    }

                    Pane {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        padding: 0
                        Material.background: "#141a24"
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 0
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.margins: 14
                                Label { text: backend.activeView === "logs" ? "DESTINATION" : "NAME"; color: "#8793a5"; font.pixelSize: 10; font.bold: true; Layout.fillWidth: true; Layout.preferredWidth: 220 }
                                Label { visible: backend.activeView !== "credentials"; text: backend.activeView === "logs" ? "VM · PORT · TIME" : backend.activeView === "rules" ? "HOST · VM SELECTOR" : "IP ADDRESS"; color: "#8793a5"; font.pixelSize: 10; font.bold: true; Layout.fillWidth: true; Layout.preferredWidth: 260 }
                                Label { text: backend.activeView === "rules" ? "ACTION" : backend.activeView === "credentials" ? "LIVE STATUS" : backend.activeView === "vms" ? "STATUS" : "DECISION"; color: "#8793a5"; font.pixelSize: 10; font.bold: true; Layout.preferredWidth: 112 }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: "#2b3544" }
                            ListView {
                                id: inventory
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                model: backend.currentModel
                                delegate: ItemDelegate {
                                    id: rowDelegate
                                    required property var item
                                    required property int index
                                    property string itemKey: root.rowKey(item)
                                    width: ListView.view.width
                                    highlighted: itemKey === backend.selectionKey
                                    onClicked: backend.selectItem(backend.activeView, itemKey)
                                    background: Rectangle { color: rowDelegate.highlighted ? "#1b2935" : index % 2 ? "#141a24" : "#121821" }
                                    contentItem: RowLayout {
                                        spacing: 12
                                        Label { Layout.fillWidth: true; Layout.preferredWidth: 220; text: root.rowPrimary(rowDelegate.item); color: "#edf2f7"; font.bold: true; elide: Text.ElideRight }
                                        Label { visible: backend.activeView !== "credentials"; Layout.fillWidth: true; Layout.preferredWidth: 260; text: root.rowSecondary(rowDelegate.item); color: "#8793a5"; font.pixelSize: 11; elide: Text.ElideRight; font.family: backend.activeView === "vms" ? "monospace" : "" }
                                        Label { Layout.preferredWidth: 112; text: root.rowStatus(rowDelegate.item); color: root.rowTone(rowDelegate.item); font.pixelSize: 11; elide: Text.ElideRight }
                                    }
                                }
                                Label {
                                    anchors.centerIn: parent
                                    visible: backend.currentModel.count === 0
                                    text: backend.activeView === "logs" ? "No connections logged yet." : backend.activeView === "vms" ? "No agent VMs registered yet." : backend.activeView === "rules" ? "No rules yet." : "No credentials yet."
                                    color: "#8793a5"
                                }
                            }
                        }
                    }
                }

                // Right inspector
                Inspector {
                    Layout.fillHeight: true
                    Layout.preferredWidth: 355
                    backend: root.controller
                    onConfirmationRequested: (kind, name, action) => root.showConfirmation(kind, name, action)
                }
            }
        }
    }

    EditorDrawer { id: editorDrawer; backend: root.controller }

    Dialog {
        id: confirmation
        property string kind: ""
        property string name: ""
        property string action: ""
        modal: true
        title: action === "delete" ? "Delete " + name + "?" : action.charAt(0).toUpperCase() + action.slice(1) + " " + name + "?"
        standardButtons: Dialog.Yes | Dialog.No
        Overlay.modal: Rectangle { color: "#b3000000" }
        Label {
            width: 360
            wrapMode: Text.Wrap
            text: confirmation.action === "delete" ? "This removes the VM and its LXD instance. This action cannot be undone." : "Jetty will " + confirmation.action + " this VM."
            color: "#aab5c4"
        }
        onAccepted: {
            if (kind === "vm") {
                if (action === "delete") backend.deleteVm(name)
                else backend.vmAction(name, action)
            } else backend.deleteItem(kind, name)
        }
    }

    Timer { id: toastTimer; interval: 7000; onTriggered: backend.clearMessage() }
    Connections {
        target: backend
        function onMessageChanged() { toastTimer.restart() }
    }
}
