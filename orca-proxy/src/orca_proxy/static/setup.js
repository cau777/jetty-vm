const statusBox = document.querySelector("#setup-status");
const resultBox = document.querySelector("#setup-result");
const setupButton = document.querySelector("#setup-button");

async function readStatus() {
  try {
    const response = await fetch("/api/v1/status");
    const status = await response.json();
    if (!response.ok) throw new Error(status?.error?.message || `HTTP ${response.status}`);
    if (!status.lxd_available) {
      statusBox.className = "banner warn";
      statusBox.textContent = "LXD is not available to this login. Run `jetty setup`, then sign out and back in. If it remains unavailable, reboot so the background service receives the lxd group.";
      setupButton.disabled = true;
      return;
    }
    statusBox.className = status.proxy === "running" ? "banner success" : "banner info";
    statusBox.textContent = status.proxy === "running"
      ? "LXD is available and the proxy is running."
      : "LXD is available. Jetty will start the proxy after the gateway network is created.";
    setupButton.disabled = false;
  } catch (error) {
    statusBox.className = "banner danger";
    statusBox.textContent = `Could not reach Jetty: ${error.message}`;
    setupButton.disabled = true;
  }
}

setupButton.addEventListener("click", async () => {
  setupButton.disabled = true;
  setupButton.textContent = "Creating gateway…";
  resultBox.textContent = "LXD may need to download the Ubuntu image on its first run.";
  resultBox.className = "setup-result muted";
  try {
    const key = document.querySelector("#ssh-public-key").value.trim();
    const response = await fetch("/api/v1/jetty/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(key ? { ssh_public_key: key } : {}),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body?.error?.message || `HTTP ${response.status}`);
    resultBox.className = "setup-result success";
    resultBox.textContent = "Jetty gateway is ready. Opening the management screen…";
    await readStatus();
    window.setTimeout(() => { window.location.href = "/"; }, 900);
  } catch (error) {
    resultBox.className = "setup-result danger";
    resultBox.textContent = error.message;
    setupButton.disabled = false;
  } finally {
    setupButton.textContent = "Create or repair gateway";
  }
});

readStatus();
