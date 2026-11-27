export type View = "logs" | "rules" | "credentials" | "vm";

export interface Port {
  vm_name: string;
  vm_port: number;
  host_port: number;
  persistent: boolean;
  state: string;
  error?: string;
}

export interface Job {
  kind: string;
  state: "submitting" | "queued" | "running" | "done" | "failed";
  steps?: Array<{ label: string; status: string }>;
  error_message?: string;
  vm?: Vm;
}

export interface Vm {
  name: string;
  ip_address: string;
  instance_type: string;
  status: string;
  cpus?: string;
  memory?: string;
  disk?: string;
}

export interface Status {
  state: string;
  daemon: string;
  lxd_available: boolean;
  network_ready: boolean;
  gateway: string;
  gateway_service: string;
  gateway_agent_service: string;
  gateway_agent_version: string;
  vms: Vm[];
  message?: string;
}

export interface Rule {
  name: string;
  scope: string;
  priority: number;
  hostname: string;
  vm_selector: { type: string; vms?: string[] };
  action: {
    type: string;
    credential?: string;
    path_prefix?: string;
    injection?: { type: string; username?: string; name?: string; service?: string; region?: string };
  };
}

export interface Credential {
  name: string;
  command: string;
  ttl_seconds: number;
  status: string;
  failure_category?: string;
  last_success_at: number;
  has_last_success_at: boolean;
}

export interface QuickAddEntry {
  key: string;
  display_name: string;
  command: string;
  ttl_seconds: number;
  notes: string;
}

export interface ConnectionLog {
  id: string;
  started_at: string;
  vm_name: string;
  destination_ip: string;
  destination_port: number;
  destination_hostname: string;
  intercepted: boolean;
  outcome: string;
  duration_ms: string;
  has_duration_ms: boolean;
  sni_present: boolean;
  ech_present: boolean;
  matched_rule?: object;
  intercepted_by_rule?: object;
}

export interface RequestDetail {
  connection: ConnectionLog;
  http_requests: Array<{
    id: string;
    occurred_at: string;
    method: string;
    path: string;
    status: number;
    has_status: boolean;
    status_origin: string;
    latency_ms: string;
    has_latency_ms: boolean;
    matched_rule?: object;
    outcome: string;
    matched_credential?: string;
    trace: Array<{ rule_name: string; scope: string; priority: number; action_type: string; result: string }>;
    headers: Array<{ name: string; value: string; redacted: boolean; redaction_reason: string }>;
  }>;
}

declare global {
  interface Window {
    go?: {
      main?: {
        App?: Record<string, (...args: unknown[]) => Promise<unknown>>;
      };
    };
    runtime?: {
      EventsOn: (eventName: string, cb: (data: unknown) => void) => void;
    };
  }
}
