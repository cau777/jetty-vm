CREATE TABLE reverse_forwards (
    vm_name TEXT NOT NULL REFERENCES vms(name) ON DELETE CASCADE,
    vm_port INTEGER NOT NULL,
    host_port INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (vm_name, vm_port)
);
