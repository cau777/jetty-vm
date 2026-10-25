import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ApplicationWindow {
    id: root
    property var controller: backend
    width: 1380
    height: 820
    minimumWidth: 1100
    minimumHeight: 620
    visible: true
    title: "Jetty"
    color: "#171613"
    Material.theme: Material.Dark
    Material.accent: "#dfba80"
    Material.primary: "#201e1a"
    Material.background: "#171613"

    property var views: [
        {key: "logs", label: "Logs", glyph: "≋"},
        {key: "rules", label: "Global rules", glyph: "◇"},
        {key: "credentials", label: "Credentials", glyph: "▤"}
    ]

    function rowKey(row) { return backend.activeView === "logs" ? String(row.id) : String(row.name || "") }
    function rowPrimary(row) {
        if (backend.activeView === "logs") return row.destination_hostname || row.destination_ip || "Unknown destination"
        return row.name || ""
    }
    function rowSecondary(row) {
        if (backend.activeView === "logs") return row.vm_name + " · " + row.destination_port + " · " + String(row.started_at || "").replace("T", " ").slice(0, 19)
        if (backend.activeView === "rules") return row.hostname
        return row.ip_address || "No IP address"
    }
    function rowStatus(row) {
        if (backend.activeView === "logs") {
            if (row.intercepted) return "Intercepted"
            var outcome = String(row.outcome || "")
            if (outcome.indexOf("block_") === 0) return "Blocked"
            if (outcome.indexOf("allow_") === 0) return "Allowed"
            return outcome.replace(/_/g, " ") || "Unknown"
        }
        if (backend.activeView === "rules") return row.action ? row.action.type.replace(/_/g, " ") : "unknown"
        if (backend.activeView === "credentials") return row.status || "empty"
        return row.status || "unknown"
    }
    function rowTone(row) {
        if (backend.activeView === "logs") {
            if (row.intercepted) return "#96bce8"
            var outcome = String(row.outcome || "")
            if (outcome.indexOf("block_") === 0) return "#ff7b7b"
            if (outcome.indexOf("allow_") === 0) return "#9bc6a4"
            return "#afa698"
        }
        if (backend.activeView === "credentials") return row.status === "valid" ? "#dfba80" : row.status === "error" ? "#ff7b7b" : "#dfba80"
        if (backend.activeView === "vms") return row.status === "Running" ? "#dfba80" : "#dfba80"
        return row.action && row.action.type === "block" ? "#ff7b7b" : row.action && row.action.type === "allow_with_credential" ? "#96bce8" : "#dfba80"
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
            Label { text: "JETTY"; font.pixelSize: 15; font.bold: true; color: "#f1ece3"; Layout.leftMargin: 20 }
            Item { Layout.fillWidth: true }
            Rectangle { width: 8; height: 8; radius: 4; color: backend.proxyState === "running" ? "#dfba80" : backend.proxyState === "failed" ? "#ff7b7b" : "#dfba80" }
            Label { text: "proxy " + backend.proxyState; color: "#c3bcae"; font.pixelSize: 11 }
            Label { visible: backend.gatewayState !== "unknown"; text: "gateway " + backend.gatewayState; color: "#afa698"; font.pixelSize: 11 }
            Label { visible: backend.tunnelState !== "unknown"; text: backend.tunnelState; color: "#afa698"; font.pixelSize: 11; Layout.rightMargin: 22 }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Left navigation
            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 235
                color: "#1c1a17"
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 5
                    Label { text: "MANAGEMENT"; color: "#766f62"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.2; Layout.leftMargin: 9; Layout.bottomMargin: 7 }
                    Repeater {
                        model: root.views
                        delegate: ItemDelegate {
                            required property var modelData
                            Layout.fillWidth: true
                            highlighted: backend.activeView === modelData.key
                            text: modelData.glyph + "    " + modelData.label
                            onClicked: backend.setView(modelData.key)
                            background: Rectangle { radius: 7; color: highlighted ? "#3a3022" : "transparent" }
                        }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#3b362c"; Layout.topMargin: 12; Layout.bottomMargin: 10 }
                    JettyButton { Layout.fillWidth: true; text: "New environment"; iconText: "＋"; highlighted: true; enabled: backend.ready && !backend.needsSetup; onClicked: backend.openEditor("vms", "") }
                    ListView {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.topMargin: 8
                        clip: true
                        model: backend.vmsModel
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: ItemDelegate {
                            id: vmNav
                            required property var item
                            width: ListView.view.width
                            height: 42
                            highlighted: backend.activeView === "vms" && backend.selectionKey === item.name
                            onClicked: backend.selectItem("vms", item.name)
                            background: Rectangle { radius: 7; color: vmNav.highlighted ? "#3a3022" : vmNav.hovered ? "#29261f" : "transparent" }
                            contentItem: RowLayout {
                                spacing: 10
                                Rectangle { width: 7; height: 7; radius: 4; color: vmNav.item.status === "Running" ? "#dfba80" : vmNav.item.status === "Stopped" ? "#afa698" : "#dfba80" }
                                Label { Layout.fillWidth: true; text: vmNav.item.name; elide: Text.ElideRight; color: vmNav.highlighted ? "#f1ece3" : "#c3bcae"; font.pixelSize: 12 }
                            }
                            ToolTip.visible: hovered
                            ToolTip.text: item.name + " · " + (item.status || "Unknown")
                        }
                    }
                    Label { text: backend.ready ? "● daemon ready" : "● daemon unavailable"; color: backend.ready ? "#dfba80" : "#dfba80"; font.pixelSize: 11; Layout.leftMargin: 9 }
                    Label { text: "LXD " + (backend.lxdAvailable ? "available" : "unavailable"); color: "#afa698"; font.pixelSize: 10; Layout.leftMargin: 9; Layout.bottomMargin: 5 }
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

                // Logs, global rules and credentials retain their table and inspector.
                ColumnLayout {
                    visible: backend.activeView !== "vms"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.margins: 26
                    spacing: 15

                    RowLayout {
                        Layout.fillWidth: true
                        // Fills and elides so the table, not the inspector, gives way on narrow windows.
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Label { text: backend.activeView === "logs" ? "ACTIVITY" : "INVENTORY"; color: "#dfba80"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.4 }
                            Label { Layout.fillWidth: true; text: backend.viewTitle + "  " + backend.currentModel.count; font.pixelSize: 22; font.bold: true; elide: Text.ElideRight }
                            Label { visible: backend.activeView !== "logs" && backend.viewSubtitle !== ""; Layout.fillWidth: true; text: backend.viewSubtitle; color: "#afa698"; font.pixelSize: 12; elide: Text.ElideRight }
                        }
                        JettyButton { text: "Refresh"; iconText: "↻"; flat: true; onClicked: backend.refresh() }
                        JettyButton {
                            visible: backend.activeView !== "logs"
                            text: backend.activeView === "vms" ? "New environment" : backend.activeView === "rules" ? "New rule" : "New credential"
                            iconText: "＋"
                            highlighted: true
                            onClicked: backend.openEditor(backend.activeView, "")
                        }
                    }

                    Label {
                        visible: backend.message !== ""
                        Layout.fillWidth: true
                        text: backend.message
                        color: backend.messageTone === "danger" ? "#ff7b7b" : backend.messageTone === "success" ? "#dfba80" : "#96bce8"
                        wrapMode: Text.Wrap
                    }

                    Pane {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        padding: 0
                        Material.background: "#201e1a"
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 0
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.margins: backend.activeView === "logs" ? 12 : 14
                                Layout.preferredHeight: backend.activeView === "logs" ? 32 : 42
                                Label { text: backend.activeView === "logs" ? "DESTINATION" : "NAME"; color: "#afa698"; font.pixelSize: 10; font.bold: true; Layout.fillWidth: true; Layout.preferredWidth: backend.activeView === "logs" ? 190 : 220 }
                                Label { visible: backend.activeView !== "credentials"; text: backend.activeView === "logs" ? "VM · PORT · TIME" : backend.activeView === "rules" ? "HOST" : "IP ADDRESS"; color: "#afa698"; font.pixelSize: 10; font.bold: true; Layout.fillWidth: true; Layout.preferredWidth: 260 }
                                Label { text: backend.activeView === "rules" ? "ACTION" : backend.activeView === "credentials" ? "LIVE STATUS" : backend.activeView === "vms" ? "STATUS" : backend.activeView === "logs" ? "OUTCOME" : "DECISION"; color: "#afa698"; font.pixelSize: 10; font.bold: true; Layout.preferredWidth: backend.activeView === "logs" ? 112 : 112 }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: "#3b362c" }
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
                                    height: backend.activeView === "logs" ? 38 : 48
                                    highlighted: itemKey === backend.selectionKey
                                    onClicked: backend.selectItem(backend.activeView, itemKey)
                                    background: Rectangle { color: rowDelegate.highlighted ? "#3a3022" : index % 2 ? "#201e1a" : "#1b1915" }
                                    contentItem: RowLayout {
                                        spacing: backend.activeView === "logs" ? 8 : 12
                                        Label { Layout.fillWidth: true; Layout.preferredWidth: backend.activeView === "logs" ? 190 : 220; text: root.rowPrimary(rowDelegate.item); color: "#f1ece3"; font.bold: true; font.pixelSize: backend.activeView === "logs" ? 12 : 13; elide: Text.ElideRight }
                                        Label { visible: backend.activeView !== "credentials"; Layout.fillWidth: true; Layout.preferredWidth: 260; text: root.rowSecondary(rowDelegate.item); color: "#afa698"; font.pixelSize: 11; elide: Text.ElideRight; font.family: backend.activeView === "vms" || backend.activeView === "logs" ? "monospace" : "" }
                                        Rectangle {
                                            visible: backend.activeView === "logs"
                                            Layout.preferredWidth: 104
                                            Layout.preferredHeight: 23
                                            radius: 6
                                            color: rowDelegate.item.intercepted ? "#202d3c" : String(rowDelegate.item.outcome || "").indexOf("block_") === 0 ? "#352320" : String(rowDelegate.item.outcome || "").indexOf("allow_") === 0 ? "#1d2c22" : "#29261f"
                                            border.color: rowDelegate.item.intercepted ? "#344b60" : String(rowDelegate.item.outcome || "").indexOf("block_") === 0 ? "#583330" : String(rowDelegate.item.outcome || "").indexOf("allow_") === 0 ? "#344b38" : "#3b362c"
                                            Label { anchors.centerIn: parent; text: root.rowStatus(rowDelegate.item); color: root.rowTone(rowDelegate.item); font.pixelSize: 10; font.bold: true }
                                        }
                                        Label { visible: backend.activeView !== "logs"; Layout.preferredWidth: 112; text: root.rowStatus(rowDelegate.item); color: root.rowTone(rowDelegate.item); font.pixelSize: 11; elide: Text.ElideRight }
                                    }
                                }
                                Label {
                                    anchors.centerIn: parent
                                    visible: backend.currentModel.count === 0
                                    text: backend.activeView === "logs" ? "No connections logged yet." : backend.activeView === "vms" ? "No agent VMs registered yet." : backend.activeView === "rules" ? "No rules yet." : "No credentials yet."
                                    color: "#afa698"
                                }
                            }
                        }
                    }
                }

                VmPage {
                    visible: backend.activeView === "vms"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.margins: 26
                    backend: root.controller
                    onConfirmationRequested: (kind, name, action) => root.showConfirmation(kind, name, action)
                }

                // Right inspector
                Inspector {
                    visible: backend.activeView !== "vms"
                    Layout.fillHeight: true
                    Layout.preferredWidth: 410
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
            text: confirmation.action === "delete" ? (confirmation.kind === "vm" ? "This removes the VM and its LXD instance. This action cannot be undone." : "Delete this " + (confirmation.kind === "rules" ? "rule" : "credential") + "?") : "Jetty will " + confirmation.action + " this VM."
            color: "#c3bcae"
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
