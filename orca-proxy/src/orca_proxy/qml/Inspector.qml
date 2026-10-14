import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

Pane {
    id: root
    required property var backend
    signal confirmationRequested(string kind, string name, string action)
    property bool createVmToHost: true
    property var portConnectionRows: {
        var vmName = root.backend.selection.name
        var rows = []
        var forwards = root.backend.portForwards || []
        var reverseForwards = root.backend.reverseForwards || []
        for (var i = 0; i < forwards.length; i++) {
            if (forwards[i].vm_name === vmName)
                rows.push({connection: forwards[i], reverse: false})
        }
        for (var j = 0; j < reverseForwards.length; j++) {
            if (reverseForwards[j].vm_name === vmName)
                rows.push({connection: reverseForwards[j], reverse: true})
        }
        return rows
    }
    padding: 18
    Material.background: "#1c1a17"
    background: Rectangle { color: "#201e1a"; radius: 10; border.color: "#3b362c" }

    // A compact row for either port-forward direction.
    component PortRow: ColumnLayout {
        id: portRow
        property var connection: ({})
        property bool reverse: false
        property bool last: false
        signal closeRequested()
        signal persistentToggled(bool checked)
        Layout.fillWidth: true
        spacing: 4
        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            Rectangle {
                width: 7; height: 7; radius: 4
                color: portRow.connection.error || portRow.connection.state === "retrying" ? "#ff7b7b" : "#dfba80"
            }
            Label {
                Layout.fillWidth: true
                text: "VM(" + portRow.connection.vm_port + ") " + (portRow.reverse ? "←" : "→") + " HOST(" + portRow.connection.host_port + ")"
                font.family: "monospace"
                font.pixelSize: 12
                font.bold: true
                color: "#f1ece3"
                elide: Text.ElideRight
            }
            CheckBox {
                text: "Start with Jetty"
                checked: !!portRow.connection.persistent
                padding: 0
                spacing: 4
                font.pixelSize: 10
                Layout.minimumWidth: 100
                onToggled: portRow.persistentToggled(checked)
            }
            Item {
                visible: portRow.reverse || !portRow.connection.url
                Layout.preferredWidth: 76
                Layout.preferredHeight: 30
            }
            JettyButton {
                visible: !portRow.reverse && !!portRow.connection.url
                Layout.preferredWidth: 76
                text: "Open"; iconText: "↗"; flat: true; implicitHeight: 30
                onClicked: Qt.openUrlExternally(portRow.connection.url)
            }
            JettyButton { Layout.preferredWidth: 76; text: "Close"; iconText: "×"; flat: true; implicitHeight: 30; onClicked: portRow.closeRequested() }
        }
        Label {
            Layout.fillWidth: true
            visible: !!portRow.connection.error
            text: portRow.connection.error || ""
            color: "#ff9a9a"
            wrapMode: Text.Wrap
            font.pixelSize: 11
        }
        Rectangle { visible: !portRow.last; Layout.fillWidth: true; height: 1; color: "#3b362c" }
    }

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: 12

            Label { text: root.backend.activeView === "vms" ? "PORT CONNECTIONS" : "SELECTION INSPECTOR"; color: "#dfba80"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.1 }
            Label {
                Layout.fillWidth: true
                visible: !root.backend.selection.name && root.backend.activeView !== "logs"
                text: "Select a row to inspect it."
                color: "#afa698"
                wrapMode: Text.Wrap
            }
            Label {
                Layout.fillWidth: true
                visible: root.backend.activeView === "logs" && !root.backend.selection.id
                text: "Select a connection to inspect its rule evaluation and HTTP requests."
                color: "#afa698"
                wrapMode: Text.Wrap
            }

            // VM port connections
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "vms" && !!root.backend.selection.name
                spacing: 8
                Repeater {
                    model: root.portConnectionRows
                    delegate: PortRow {
                        required property var modelData
                        required property int index
                        connection: modelData.connection
                        reverse: modelData.reverse
                        last: index === root.portConnectionRows.length - 1
                        onCloseRequested: {
                            if (reverse) root.backend.closeReversePort(connection.vm_name, connection.vm_port)
                            else root.backend.closePort(connection.vm_name, connection.host_port)
                        }
                        onPersistentToggled: checked => {
                            if (reverse) root.backend.setReversePortPersistent(connection.vm_name, connection.vm_port, checked)
                            else root.backend.setPortPersistent(connection.vm_name, connection.host_port, checked)
                        }
                    }
                }

                Label {
                    visible: root.portConnectionRows.length === 0
                    Layout.fillWidth: true
                    text: "No port connections yet."
                    color: "#afa698"
                    font.pixelSize: 11
                }

                Rectangle { Layout.fillWidth: true; height: 1; color: "#3b362c"; Layout.topMargin: 12; Layout.bottomMargin: 12 }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6
                    TextField {
                        id: vmPortInput
                        Layout.fillWidth: true
                        Layout.minimumWidth: 70
                        placeholderText: "VM port"
                        implicitHeight: 36
                        leftPadding: 9; rightPadding: 9; topPadding: 0; bottomPadding: 0
                        font.pixelSize: 13
                        inputMethodHints: Qt.ImhDigitsOnly
                        validator: IntValidator { bottom: root.createVmToHost ? 1 : 1024; top: 65535 }
                        onAccepted: addPortButton.clicked()
                    }
                    JettyButton {
                        text: root.createVmToHost ? "→" : "←"
                        font.pixelSize: 17
                        flat: true
                        implicitWidth: 40
                        implicitHeight: 36
                        ToolTip.visible: hovered
                        ToolTip.text: "Reverse connection direction"
                        onClicked: root.createVmToHost = !root.createVmToHost
                    }
                    TextField {
                        id: hostPortInput
                        Layout.fillWidth: true
                        Layout.minimumWidth: 70
                        placeholderText: "Host port"
                        implicitHeight: 36
                        leftPadding: 9; rightPadding: 9; topPadding: 0; bottomPadding: 0
                        font.pixelSize: 13
                        inputMethodHints: Qt.ImhDigitsOnly
                        validator: IntValidator { bottom: root.createVmToHost ? 1024 : 1; top: 65535 }
                        onAccepted: addPortButton.clicked()
                    }
                    JettyButton {
                        id: addPortButton
                        text: "Add"
                        iconText: "＋"
                        enabled: root.createVmToHost
                            ? vmPortInput.acceptableInput && (hostPortInput.text === "" || hostPortInput.acceptableInput)
                            : hostPortInput.acceptableInput && (vmPortInput.text === "" || vmPortInput.acceptableInput)
                        onClicked: {
                            if (root.createVmToHost)
                                root.backend.openPort(root.backend.selection.name, vmPortInput.text, hostPortInput.text, persistentPort.checked)
                            else
                                root.backend.openReversePort(root.backend.selection.name, hostPortInput.text, vmPortInput.text, persistentPort.checked)
                            vmPortInput.clear()
                            hostPortInput.clear()
                            persistentPort.checked = false
                        }
                    }
                }
                CheckBox {
                    id: persistentPort
                    text: "Start with Jetty"
                    padding: 0
                    font.pixelSize: 10
                }
            }

            // Rule details
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "rules" && !!root.backend.selection.name
                spacing: 9
                Label { text: root.backend.selection.name || ""; font.pixelSize: 19; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                Label { text: "Priority " + (root.backend.selection.priority === undefined ? "—" : root.backend.selection.priority) + " · " + (root.backend.selection.hostname || ""); color: "#c3bcae"; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                Label {
                    Layout.fillWidth: true
                    text: "VMs: " + (root.backend.selection.vm_selector && root.backend.selection.vm_selector.type === "all" ? "all" : (root.backend.selection.vm_selector && root.backend.selection.vm_selector.vms || []).join(", "))
                    color: "#afa698"
                    wrapMode: Text.Wrap
                }
                Label { Layout.fillWidth: true; text: JSON.stringify(root.backend.selection.action || {}); color: "#96bce8"; wrapMode: Text.WrapAnywhere; font.family: "monospace"; font.pixelSize: 11 }
                RowLayout {
                    JettyButton { text: "Edit"; iconText: "✎"; flat: true; onClicked: root.backend.openEditor("rules", root.backend.selection.name) }
                    JettyButton { text: "Delete"; iconText: "⌫"; destructive: true; onClicked: root.confirmationRequested("rules", root.backend.selection.name, "delete") }
                }
            }

            // Credential details
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "credentials" && !!root.backend.selection.name
                spacing: 9
                Label { text: root.backend.selection.name || ""; font.pixelSize: 19; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                Label { text: "Live status: " + (root.backend.selection.status || "unknown"); color: root.backend.selection.status === "valid" ? "#dfba80" : root.backend.selection.status === "error" ? "#ff7b7b" : "#dfba80" }
                Label { text: "Cache TTL: " + (root.backend.selection.ttl_seconds === 0 ? "no cache" : (root.backend.selection.ttl_seconds || 0) + " seconds"); color: "#c3bcae" }
                Label { text: "Provider command"; color: "#afa698"; font.pixelSize: 11 }
                TextArea { Layout.fillWidth: true; readOnly: true; text: root.backend.selection.command || ""; wrapMode: TextEdit.Wrap; selectByMouse: true; font.family: "monospace"; font.pixelSize: 11; implicitHeight: 110 }
                Label { Layout.fillWidth: true; text: root.backend.selection.failure_category ? "Last failure: " + root.backend.selection.failure_category : root.backend.selection.last_success_at ? "Last success: " + root.backend.selection.last_success_at : "No cached credential value"; color: "#afa698"; wrapMode: Text.Wrap; font.pixelSize: 11 }
                RowLayout {
                    JettyButton { text: "Edit"; iconText: "✎"; flat: true; onClicked: root.backend.openEditor("credentials", root.backend.selection.name) }
                    JettyButton { text: "Clear cache"; iconText: "↻"; flat: true; onClicked: root.backend.refreshCredential(root.backend.selection.name) }
                    JettyButton { text: "Delete"; iconText: "⌫"; destructive: true; onClicked: root.confirmationRequested("credentials", root.backend.selection.name, "delete") }
                }
                Label { Layout.fillWidth: true; text: "Credential values and command output are never shown here."; color: "#afa698"; wrapMode: Text.Wrap; font.pixelSize: 11 }
            }

            // Connection details and each intercepted HTTP request
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "logs" && !!root.backend.selection.id
                spacing: 10
                Label { text: root.backend.selection.destination_hostname || root.backend.selection.destination_ip || "Connection"; font.pixelSize: 17; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                Label { text: root.backend.selection.vm_name || ""; color: "#96bce8" }
                Label { Layout.fillWidth: true; text: (root.backend.selection.destination_ip || "") + ":" + (root.backend.selection.destination_port || ""); color: "#c3bcae"; font.family: "monospace" }
                Label { Layout.fillWidth: true; text: "Started " + (root.backend.selection.started_at || "—") + " · " + (root.backend.selection.duration_ms === null ? "—" : (root.backend.selection.duration_ms + " ms")); color: "#afa698"; wrapMode: Text.Wrap }
                Label { Layout.fillWidth: true; text: "Decision: " + (root.backend.selection.outcome || "unknown") + (root.backend.selection.intercepted ? " · intercepted" : ""); color: "#c3bcae"; wrapMode: Text.Wrap }
                Label { Layout.fillWidth: true; text: "SNI " + (root.backend.selection.sni_present ? "present" : "absent") + " · ECH " + (root.backend.selection.ech_present ? "present" : "absent"); color: "#afa698"; wrapMode: Text.Wrap }
                Label { visible: !!root.backend.selection.matched_rule; text: "Matched rule"; color: "#afa698"; font.pixelSize: 11 }
                Label { Layout.fillWidth: true; visible: !!root.backend.selection.matched_rule; text: JSON.stringify(root.backend.selection.matched_rule || {}); color: "#96bce8"; wrapMode: Text.WrapAnywhere; font.family: "monospace"; font.pixelSize: 10 }
                Label { visible: !!root.backend.selection.intercepted_by_rule; text: "Intercepted by"; color: "#afa698"; font.pixelSize: 11 }
                Label { Layout.fillWidth: true; visible: !!root.backend.selection.intercepted_by_rule; text: JSON.stringify(root.backend.selection.intercepted_by_rule || {}); color: "#96bce8"; wrapMode: Text.WrapAnywhere; font.family: "monospace"; font.pixelSize: 10 }
                Rectangle { Layout.fillWidth: true; height: 1; color: "#3b362c" }
                Label { text: "HTTP requests"; font.bold: true }
                Repeater {
                    model: root.backend.logDetails.http_requests || []
                    delegate: Pane {
                        required property var modelData
                        Layout.fillWidth: true
                        padding: 10
                        Material.background: "#171613"
                        ColumnLayout {
                            width: parent.width
                            spacing: 5
                            Label { Layout.fillWidth: true; text: modelData.method + " " + modelData.path; font.family: "monospace"; font.bold: true; wrapMode: Text.WrapAnywhere }
                            Label { Layout.fillWidth: true; text: "Outcome " + modelData.outcome + " · status " + (modelData.status === null ? "—" : modelData.status) + " (" + (modelData.status_origin || "unknown") + ") · " + (modelData.latency_ms === null ? "—" : modelData.latency_ms + " ms"); color: "#c3bcae"; wrapMode: Text.Wrap }
                            Label { Layout.fillWidth: true; text: "Credential: " + (modelData.matched_credential || "none") + " · rule: " + JSON.stringify(modelData.matched_rule || {}); color: "#afa698"; wrapMode: Text.WrapAnywhere; font.pixelSize: 10 }
                            Label { text: "Rule evaluation trace"; color: "#afa698"; font.pixelSize: 10 }
                            TextArea { Layout.fillWidth: true; readOnly: true; text: JSON.stringify(modelData.trace || [], null, 2); wrapMode: TextEdit.Wrap; selectByMouse: true; font.family: "monospace"; font.pixelSize: 9; implicitHeight: Math.max(90, Math.min(180, 35 + (modelData.trace || []).length * 23)) }
                            Label { text: "Redacted headers"; color: "#afa698"; font.pixelSize: 10 }
                            TextArea { Layout.fillWidth: true; readOnly: true; text: JSON.stringify(modelData.headers || [], null, 2); wrapMode: TextEdit.Wrap; selectByMouse: true; font.family: "monospace"; font.pixelSize: 9; implicitHeight: Math.max(80, Math.min(150, 35 + (modelData.headers || []).length * 20)) }
                        }
                    }
                }
                Label { Layout.fillWidth: true; visible: root.backend.logDetails.http_requests !== undefined && root.backend.logDetails.http_requests.length === 0; text: "This connection did not contain intercepted HTTP requests."; color: "#afa698"; wrapMode: Text.Wrap }
            }
        }
    }
}
