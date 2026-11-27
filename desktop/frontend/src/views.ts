import type { ConnectionLog, Credential, Job, Port, QuickAddEntry, RequestDetail, Rule, Status, Vm, View } from "./types";

export function esc(value: unknown): string {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

export function button(glyph: string, label: string, attributes = "", style = "", disabled = false): string {
  return `<button type="button" class="button ${style}" ${attributes} ${disabled ? "disabled" : ""}>${glyph ? `<span aria-hidden="true">${glyph}</span> ` : ""}${esc(label)}</button>`;
}

const notice = '<div class="notice" role="status"></div>';
const pretty = (value: string) => value.replaceAll("_", " ");
const json = (value: unknown) => esc(JSON.stringify(value, null, 2));

function logRow(item: ConnectionLog, selected: boolean) {
  const tone = item.intercepted ? "intercept" : item.outcome.startsWith("block_") ? "block"
    : item.outcome.startsWith("allow_") ? "allow" : "";
  const outcome = { intercept: "Intercepted", block: "Blocked", allow: "Allowed", "": pretty(item.outcome) || "Unknown" }[tone];
  const subtitle = `${item.vm_name} · ${item.destination_port} · ${item.started_at.replace("T", " ").slice(0, 19)}`;
  return `<button class="row log ${selected ? "selected" : ""}" data-log="${esc(item.id)}" aria-pressed="${selected}">
    <strong>${esc(item.destination_hostname || item.destination_ip || "Unknown destination")}</strong>
    <code>${esc(subtitle)}</code><span class="badge ${tone}">${esc(outcome)}</span></button>`;
}

function logInspector(connection: ConnectionLog, detail?: RequestDetail, error = "") {
  const record = detail?.connection ?? connection;
  const requests = detail?.http_requests ?? [];
  return `<h3>${esc(record.destination_hostname || record.destination_ip || "Connection")}</h3>
    <p class="blue">${esc(record.vm_name)}</p>
    <p><code>${esc(record.destination_ip)}:${record.destination_port}</code></p>
    <p class="muted">Started ${esc(record.started_at)} · ${record.has_duration_ms ? esc(record.duration_ms) + " ms" : "—"}</p>
    <p>Decision: ${esc(record.outcome || "unknown")}${record.intercepted ? " · intercepted" : ""}</p>
    <p class="muted">SNI ${record.sni_present ? "present" : "absent"} · ECH ${record.ech_present ? "present" : "absent"}</p>
    ${record.matched_rule ? `<div class="muted">Matched rule</div><pre>${json(record.matched_rule)}</pre>` : ""}
    ${record.intercepted_by_rule ? `<div class="muted">Intercepted by</div><pre>${json(record.intercepted_by_rule)}</pre>` : ""}
    <h4>HTTP requests</h4>
    ${error ? `<p class="error">${esc(error)}</p>` : !detail ? '<p class="muted">Loading request details…</p>'
      : !requests.length ? '<p class="muted">This connection did not contain intercepted HTTP requests.</p>'
      : requests.map(request => `<article class="entry">
        <strong><code>${esc(request.method)} ${esc(request.path)}</code></strong>
        <p>Outcome ${esc(request.outcome)} · status ${request.has_status ? request.status : "—"}
          (${esc(request.status_origin || "unknown")}) · ${request.has_latency_ms ? esc(request.latency_ms) + " ms" : "—"}</p>
        <p class="muted">Credential: ${esc(request.matched_credential || "none")} · rule: ${esc(JSON.stringify(request.matched_rule ?? {}))}</p>
        <div class="muted">Rule evaluation trace</div><pre tabindex="0">${json(request.trace ?? [])}</pre>
        <div class="muted">Redacted headers</div><pre tabindex="0">${json(request.headers ?? [])}</pre>
      </article>`).join("")}`;
}

function ruleInspector(rule: Rule, pending: boolean) {
  return `<h3>${esc(rule.name)}</h3><p>Priority ${rule.priority} · ${esc(rule.hostname)}</p>
    <p>VMs: ${esc(rule.vm_selector?.type === "all" ? "all" : rule.vm_selector?.vms?.join(", "))}</p>
    <pre>${json(rule.action)}</pre><div class="actions start">
      ${button("✎", "Edit", `data-edit="rules" data-name="${esc(rule.name)}"`, "flat", pending)}
      ${button("⌫", "Delete", `data-delete-rule="${esc(rule.name)}"`, "danger", pending)}</div>`;
}

function credentialInspector(credential: Credential, pending: boolean) {
  const last = credential.failure_category ? `Last failure: ${credential.failure_category}`
    : credential.has_last_success_at ? `Last success: ${new Date(credential.last_success_at * 1000).toLocaleString()}` : "No cached credential value";
  return `<h3>${esc(credential.name)}</h3><p class="${credential.status === "error" ? "error" : "accent"}">Live status: ${esc(credential.status || "unknown")}</p>
    <p>Cache TTL: ${credential.ttl_seconds ? credential.ttl_seconds + " seconds" : "no cache"}</p>
    <div class="muted">Provider command</div><pre tabindex="0">${esc(credential.command)}</pre>
    <p class="muted">${esc(last)}</p><div class="actions start">
      ${button("✎", "Edit", `data-edit="credentials" data-name="${esc(credential.name)}"`, "flat", pending)}
      ${button("↻", "Clear cache", `data-clear-cache="${esc(credential.name)}"`, "flat", pending)}
      ${button("⌫", "Delete", `data-delete-credential="${esc(credential.name)}"`, "danger", pending)}</div>
    <p class="muted">Credential values and command output are never shown here.</p>`;
}

export function inventory(view: Exclude<View, "vm">, selection: string, logs: ConnectionLog[], rules: Rule[],
  credentials: Credential[], detail: RequestDetail | undefined, detailError: string, pending: boolean) {
  const isLogs = view === "logs";
  const isRules = view === "rules";
  const title = isLogs ? "Connection logs" : isRules ? "Global rules" : "Credentials";
  const subtitle = isRules ? "Rules for all VMs, evaluated after each VM’s own rules."
    : "Manage host-side credential providers without exposing their values.";
  const count = isLogs ? logs.length : isRules ? rules.length : credentials.length;
  const rows = isLogs ? logs.map(item => logRow(item, String(item.id) === selection)) : (isRules ? rules : credentials).map(item => {
    const status = "action" in item ? pretty(item.action?.type || "unknown") : item.status || "empty";
    const tone = "action" in item ? item.action?.type === "block" ? "error"
      : item.action?.type === "allow_with_credential" ? "blue" : "accent" : item.status === "error" ? "error" : "accent";
    return `<button class="row ${view} ${selection === item.name ? "selected" : ""}" data-select="${esc(item.name)}" aria-pressed="${selection === item.name}">
      <strong>${esc(item.name)}</strong>${"hostname" in item ? `<span class="muted">${esc(item.hostname)}</span>` : ""}
      <span class="${tone}">${esc(status)}</span></button>`;
  });
  let inspector = '<p class="muted">Select a row to inspect it.</p>';
  if (isLogs) {
    const item = logs.find(row => String(row.id) === selection) ?? detail?.connection;
    inspector = item ? logInspector(item, detail, detailError) : '<p class="muted">Select a connection to inspect its rule evaluation and HTTP requests.</p>';
  } else if (isRules) {
    const item = rules.find(row => row.name === selection);
    if (item) inspector = ruleInspector(item, pending);
  } else {
    const item = credentials.find(row => row.name === selection);
    if (item) inspector = credentialInspector(item, pending);
  }
  return `<main class="inventory">
    <header class="header-row"><div><div class="eyebrow">${isLogs ? "Activity" : "Inventory"}</div>
      <h1>${title} <span class="count">${count}</span></h1>${isLogs ? "" : `<div class="muted">${subtitle}</div>`}</div>
      <div class="actions">${button("↻", "Refresh", "data-refresh", "flat")}
      ${isLogs ? "" : button("+", isRules ? "New rule" : "New credential", `data-edit="${view}"`, "primary")}</div></header>
    ${notice}
    <section class="listing">
      <div class="row columns ${isLogs ? "log" : view}"><span>${isLogs ? "Destination" : "Name"}</span>
        ${isLogs ? "<span>VM · Port · Time</span>" : isRules ? "<span>Host</span>" : ""}
        <span>${isLogs ? "Outcome" : isRules ? "Action" : "Live status"}</span></div>
      <div class="list-scroll" data-scroll="inventory">${count ? rows.join("") : `<p class="empty">No ${isLogs ? "connections logged" : isRules ? "rules" : "credentials"} yet.</p>`}</div>
    </section>
    </main><aside class="inspector" data-scroll="inspector"><div class="eyebrow">Selection inspector</div>${inspector}</aside>`;
}

function portRow(port: Port, reverse: boolean, pending: boolean) {
  const key = reverse ? port.vm_port : port.host_port;
  const direction = reverse ? "←" : "→";
  const attributes = reverse ? 'data-reverse="true"' : "";
  return `<article class="port-entry"><div class="header-row">
    <strong><i class="${port.error || port.state === "retrying" ? "failed" : ""}"></i>
      <code>VM(${port.vm_port}) ${direction} HOST(${port.host_port})</code></strong>
    <label class="check"><input type="checkbox" data-persist="${key}" ${attributes} ${port.persistent ? "checked" : ""} ${pending ? "disabled" : ""}> Start with Jetty</label>
    <div class="actions">${reverse ? "" : button("↗", "Open", `data-open-url="${port.host_port}"`, "flat")}
    ${button("×", "Close", `data-close-port="${key}" ${attributes}`, "flat", pending)}</div></div>
    ${port.error ? `<p class="error">${esc(port.error)}</p>` : ""}</article>`;
}

export function vmPage(vm: Vm | undefined, rules: Rule[], globalRules: number, ports: Port[], reverse: Port[], pending: boolean) {
  if (!vm) return '<main class="vm-page"><p class="empty">Select a VM in the sidebar, or create a new one.</p></main>';
  const running = vm.status.toLowerCase() === "running";
  const facts = [
    ["▣", "Type", vm.instance_type === "container" ? "LXD container" : "Virtual machine"],
    ["⊕", "IP address", vm.ip_address || "—"],
    ["◇", "VM rules", rules.length + globalRules],
    ["↗", "Port links", ports.length + reverse.length],
    ["▣", "CPU", vm.cpus ? `${vm.cpus} ${String(vm.cpus) === "1" ? "core" : "cores"}` : "—"],
    ["▤", "RAM", vm.memory || "—"],
    ["◫", "Disk", vm.disk || (vm.instance_type === "container" ? "No quota" : "—")],
  ];
  return `<main class="vm-page">
    <header class="header-row"><div><div class="eyebrow">Virtual machines</div><h1>${esc(vm.name)}</h1></div>
    <span class="badge vm-status">${esc(vm.status || "Unknown")}</span>
    <div class="actions">
      ${button(running ? "■" : "▶", running ? "Stop" : "Start", `data-vm-action="${running ? "stop" : "start"}"`, "", pending)}
      ${button("↻", "Restart", 'data-vm-action="restart"', "flat", pending)}
      ${button("↗", "Open files", "data-files", "primary", pending || !running)}
      ${button("⌫", "Delete", 'data-vm-action="delete"', "flat danger", pending)}</div></header>
    <div class="facts">${facts.map(([glyph, label, value]) => `<div><span class="accent">${glyph}</span><div><small>${label}</small><strong title="${esc(value)}">${esc(value)}</strong></div></div>`).join("")}</div>
    ${notice}
    <div class="vm-grid">
      <section class="panel port-panel" data-scroll="ports"><div class="eyebrow">Port connections</div>
        ${ports.map(port => portRow(port, false, pending)).join("")}${reverse.map(port => portRow(port, true, pending)).join("")}
        ${ports.length + reverse.length ? "" : '<p class="muted">No port connections yet.</p>'}
        <form data-draft="ports:${esc(vm.name)}" class="port-form"><div class="actions start">
          <input type="number" name="vm_port" aria-label="VM port" placeholder="VM port" min="1" max="65535" required>
          <button type="button" class="button flat" data-direction title="Reverse connection direction" aria-label="Reverse connection direction" aria-pressed="false">→</button>
          <input type="number" name="host_port" aria-label="Host port" placeholder="Host port" min="1024" max="65535">
          <button class="button primary" type="submit">+ Add</button></div>
          <label class="check"><input type="checkbox" name="persistent"> Start with Jetty</label>
        </form>
      </section>
      <div class="vm-side"><section class="panel"><div class="eyebrow">Agent setup</div>
        <p class="muted">The agent opens in a temporary folder and asks what you want to do with this VM.</p>
        <div class="agents">${[["claude", "Claude Code"], ["codex", "Codex"], ["pi", "Pi"], ["opencode", "OpenCode"]]
        .map(([key, name]) => button("↗", name, `data-agent="${key}"`, "", pending)).join("")}</div></section>
        <section class="panel rule-panel"><div class="header-row"><h3>VM rules</h3>${button("+", "New rule", 'data-edit="rules"', "primary")}</div>
          <div class="rule-list" data-scroll="vm-rules">${rules.length ? rules.map(rule => `<article class="entry"><div class="header-row"><strong>${esc(rule.name)}</strong><small>Priority ${rule.priority}</small></div>
            <p class="blue"><code>${esc(rule.hostname)}</code></p>
            <p>${esc([pretty(rule.action.type), rule.action.credential, rule.action.path_prefix].filter(Boolean).join(" · "))}</p>
            <div class="actions">${button("✎", "Edit", `data-edit="rules" data-name="${esc(rule.name)}"`, "flat", pending)}
            ${button("⌫", "Delete", `data-delete-rule="${esc(rule.name)}"`, "flat danger", pending)}</div></article>`).join("")
            : '<p class="muted">No VM rules yet. Global rules still apply.</p>'}</div>
          <footer class="header-row"><small>Global rules apply after these.</small>${button("◇", "Global rules", 'data-view="rules"', "flat")}</footer>
        </section></div>
    </div></main>`;
}

export function jobSteps(job: Job, elapsed: number) {
  const title = ({ submitting: "Starting operation…", queued: "Waiting to start…", running: "Working…", done: "Complete", failed: "Operation failed" })[job.state];
  return `<section class="job" aria-live="polite"><div class="header-row"><strong>${title}</strong><code data-elapsed>${elapsed} s</code></div>
    <ul>${(job.steps ?? []).map(step => `<li class="${step.status === "failed" ? "error" : step.status === "done" || step.status === "running" ? "accent" : "muted"}">
      <span>${step.status === "done" ? "✓" : step.status === "failed" ? "✕" : "○"}</span> ${esc(step.label)}</li>`).join("")}</ul>
    ${job.state === "failed" ? `<p class="error">${esc(job.error_message || "The operation failed.")}</p>` : ""}</section>`;
}

export function setup(status: Status, progress: string, busy: boolean, sshKey: string) {
  return `<main class="setup" data-scroll="setup"><div class="eyebrow">First run</div><h1>Set up the Jetty gateway</h1>
    <p>Jetty creates an isolated LXD network and gateway VM for agent egress. Existing agent VMs stay behind the gateway.</p>
    <dl><dt>LXD access</dt><dd>${status.lxd_available ? "Available" : "Unavailable"}</dd>
      <dt>Gateway</dt><dd>${esc(status.gateway || "unknown")}</dd><dt>Proxy</dt><dd>${esc(status.state)}</dd>
      <dt>Gateway service</dt><dd>${esc(status.gateway_service || "unknown")}</dd></dl>
    <p class="panel muted">${status.lxd_available
      ? "LXD group access grants root-equivalent control of the host. Only add trusted desktop users to that group."
      : "LXD is not available to this login. Run jetty setup, then sign out and back in so the service receives the lxd group. If access still fails, reboot and try again."}</p>
    <form data-draft="setup"><label>Optional SSH public key<input name="ssh_public_key" placeholder="Use the host’s default key if empty" value="${esc(sshKey)}" ${busy ? "disabled" : ""}></label>
      <button class="button primary" type="submit" ${busy || !status.lxd_available ? "disabled" : ""}>${busy ? "Creating gateway…" : "Create or repair gateway"}</button>
    </form>
    <div data-setup-progress>${progress}</div>${notice}
    ${status.message ? `<p class="muted">${esc(status.message)}</p>` : ""}
    ${status.gateway === "running" ? button("↗", "Open management dashboard", "data-dashboard", "flat") : ""}
    </main>`;
}

function option(value: string, label: string, selected?: string) {
  return `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(label)}</option>`;
}

export function editorForm(kind: string, scope: string, rule: Rule | undefined, credential: Credential | undefined,
  credentials: Credential[], catalog: QuickAddEntry[]) {
  const label = kind === "vm" ? "New environment" : kind === "rules" ? "Rule" : "Credential";
  const title = rule || credential ? `Edit ${(rule ?? credential)!.name}` : kind === "vm"
    ? "Create an agent environment" : kind === "rules" ? "Create a rule" : "Create a credential";
  let fields: string;
  let submit: string;
  if (kind === "vm") {
    submit = "Create VM";
    fields = `<label>Name<input name="name" placeholder="Lowercase letters, digits and hyphens" required></label>
      <label>Instance type<select name="instance_type"><option value="virtual-machine">Virtual machine (default)</option><option value="container">LXD container</option></select></label>
      <p class="muted" data-container-note hidden>Containers share the host kernel. The same network restrictions and credential proxy apply. Disk quotas require a supporting storage pool.</p>
      <div class="fields"><label>CPUs<input name="cpus" type="number" min="1" max="128" value="4" required></label>
      <label>Ubuntu image<input name="image" value="ubuntu:24.04" required></label></div>
      <div class="fields"><label>Memory<input name="memory" value="8GiB" required></label><label>Root disk<input name="disk" value="40GiB"></label></div>
      <label>Optional SSH public key<input name="ssh_public_key" placeholder="Use the host’s default key if empty"></label>`;
  } else if (kind === "credentials") {
    submit = "Save credential";
    fields = `${credential ? "" : `<label>Quick add a provider<select name="quick_add"><option value="">Select a provider…</option>
      ${catalog.map(item => option(item.key, item.display_name)).join("")}</select></label>`}
      <label>Credential name<input name="name" value="${esc(credential?.name)}" ${credential ? "readonly" : "required"}></label>
      <label>Provider command<textarea name="command" required placeholder="Command that prints a credential to stdout">${esc(credential?.command)}</textarea></label>
      <label>Cache TTL in seconds<input name="ttl_seconds" type="number" min="0" max="10000000" value="${credential?.ttl_seconds ?? 300}" required></label>
      <small>0 disables caching</small>`;
  } else {
    submit = "Save rule";
    const action = rule?.action;
    const injection = action?.injection;
    fields = `<label>Rule name<input name="name" value="${esc(rule?.name)}" ${rule ? "readonly" : "required"}></label>
      <label>Priority<input name="priority" type="number" min="0" max="10000000" value="${rule?.priority ?? 100}" required></label>
      <label>Hostname<input name="hostname" placeholder="Hostname, *.domain, or @aws" value="${esc(rule?.hostname)}" required></label>
      <p class="muted">${scope ? `Applies to ${esc(scope)}. Evaluated before global rules.` : "Applies to all VMs. Evaluated after each VM’s rules."}</p>
      <label>Action<select name="action_type">${[["allow", "Allow"], ["block", "Block"], ["allow_with_credential", "Allow with credential"]]
        .map(([value, caption]) => option(value, caption, action?.type)).join("")}</select></label>
      <div class="fields vertical" data-injection-fields ${action?.type === "allow_with_credential" ? "" : "hidden"}>
        <label>Path prefix<input name="path_prefix" value="${esc(action?.path_prefix || "/")}"></label>
        <label>Credential<select name="credential">${credentials.map(item => option(item.name, item.name, action?.credential)).join("")}</select></label>
        <label>Injection<select name="injection_type">${[["bearer", "Bearer"], ["basic", "Basic"], ["aws_sigv4", "AWS SigV4"], ["header", "Custom header"]]
          .map(([value, caption]) => option(value, caption, injection?.type)).join("")}</select></label>
        <label data-injection="basic" ${injection?.type === "basic" ? "" : "hidden"}>Basic auth username<input name="username" value="${esc(injection?.username)}"></label>
        <label data-injection="header" ${injection?.type === "header" ? "" : "hidden"}>Header name<input name="header_name" value="${esc(injection?.name)}"></label>
        <div class="fields vertical" data-injection="aws_sigv4" ${injection?.type === "aws_sigv4" ? "" : "hidden"}>
          <label>AWS signing service<input name="service" value="${esc(injection?.service)}"></label>
          <label>AWS signing region<input name="region" value="${esc(injection?.region)}"></label>
          <small>Use auto for both service and region to sign known regional and global AWS API endpoints.</small>
          ${button("", "Use all AWS endpoints", "data-aws")}</div>
      </div>`;
  }
  return `<div class="eyebrow">${label}</div><h2 id="editor-title">${esc(title)}</h2>
    <form data-kind="${esc(kind)}" data-scope="${esc(scope)}">${fields}
      <div class="error" role="alert"></div><div class="actions">${button("×", "Cancel", "data-close-editor", "flat")}
      <button type="submit" class="button primary">${submit}</button></div></form>
    <div data-create-progress></div>`;
}
