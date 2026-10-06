import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

Pane {
    id: root
    required property var backend
    padding: 28
    Material.background: "#141a24"

    ColumnLayout {
        anchors.fill: parent
        spacing: 16

        Label { text: "FIRST RUN"; color: "#72e0bd"; font.pixelSize: 11; font.bold: true; font.letterSpacing: 1.5 }
        Label { text: "Set up the Jetty gateway"; font.pixelSize: 24; font.bold: true }
        Label {
            Layout.fillWidth: true
            text: "Jetty creates an isolated LXD network and gateway VM for agent egress. Existing agent VMs stay behind the gateway."
            color: "#aab5c4"
            wrapMode: Text.Wrap
        }

        GridLayout {
            columns: 2
            columnSpacing: 24
            rowSpacing: 8
            Label { text: "LXD access"; color: "#8793a5" }
            Label { text: root.backend.lxdAvailable ? "Available" : "Unavailable"; color: root.backend.lxdAvailable ? "#72e0bd" : "#ffc56e" }
            Label { text: "Gateway"; color: "#8793a5" }
            Label { text: root.backend.gatewayState; color: "#edf2f7" }
            Label { text: "Proxy"; color: "#8793a5" }
            Label { text: root.backend.proxyState; color: "#edf2f7" }
            Label { text: "Tunnel"; color: "#8793a5" }
            Label { text: root.backend.tunnelState; color: "#edf2f7" }
        }

        Pane {
            Layout.fillWidth: true
            visible: !root.backend.lxdAvailable
            Material.background: "#332713"
            Label {
                width: parent.width
                text: "LXD is not available to this login. Run jetty setup, then sign out and back in so the service receives the lxd group. If access still fails, reboot and try again. LXD group access grants root-equivalent control of the host."
                color: "#ffc56e"
                wrapMode: Text.Wrap
            }
        }
        Pane {
            Layout.fillWidth: true
            visible: root.backend.lxdAvailable
            Material.background: "#332713"
            Label {
                width: parent.width
                text: "LXD group access grants root-equivalent control of the host. Only add trusted desktop users to that group."
                color: "#ffc56e"
                wrapMode: Text.Wrap
            }
        }

        Label { text: "Optional SSH public key"; color: "#8793a5"; font.pixelSize: 11 }
        TextField {
            id: sshKey
            Layout.fillWidth: true
            enabled: root.backend.job.state !== "running" && root.backend.job.state !== "queued"
            placeholderText: "Leave empty to use ~/.ssh/id_ed25519.pub"
            font.family: "monospace"
        }
        JettyButton {
            text: root.backend.job.state === "running" ? "Creating gateway…" : "Create or repair gateway"
            highlighted: true
            enabled: root.backend.lxdAvailable && root.backend.job.state !== "running" && root.backend.job.state !== "queued" && root.backend.job.state !== "submitting"
            onClicked: root.backend.startSetup(sshKey.text)
        }

        JobStepper {
            Layout.fillWidth: true
            visible: root.backend.job.kind === "gateway_setup" && root.backend.job.state !== "idle"
            job: root.backend.job
        }
        Label {
            Layout.fillWidth: true
            visible: root.backend.statusMessage !== ""
            text: root.backend.statusMessage
            color: "#8793a5"
            wrapMode: Text.Wrap
        }
        JettyButton {
            flat: true
            visible: root.backend.gatewayState === "running"
            text: "Open management dashboard"
            onClicked: root.backend.hideSetup()
        }
        Item { Layout.fillHeight: true }
    }
}
