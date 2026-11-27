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
}

export interface QuickAddEntry {
  key: string;
  display_name: string;
  command: string;
  ttl_seconds: number;
  notes: string;
}

export interface ConnectionLog {
  id: number;
  started_at: string;
  vm_name: string;
  destination_ip: string;
  destination_port: number;
  destination_hostname: string;
  intercepted: boolean;
  outcome: string;
  duration_ms?: number;
}

export interface RequestDetail {
  connection: ConnectionLog;
  http_requests: Array<{
    id: number;
    occurred_at: string;
    method: string;
    path: string;
    status: number;
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
