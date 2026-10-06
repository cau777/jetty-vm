CREATE TABLE port_forwards (
    host_port INTEGER PRIMARY KEY,
    vm_name TEXT NOT NULL REFERENCES vms(name) ON DELETE CASCADE,
    vm_port INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
