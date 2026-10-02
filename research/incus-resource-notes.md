# Incus migration: resource and installation notes

**Date:** 2026-10-02  
**Scope:** Storage, CPU/RAM budgeting, gateway sizing, installation, and VM NIC controls relevant to replacing Multipass in Jetty.  
**Sources:** Upstream Incus and Multipass documentation/source, QEMU documentation, and OpenZFS documentation. Estimates below are starting points, not measurements.

## Storage allocation

Do not compare the configured virtual disk size directly with host disk consumption. A 20 GiB virtual disk can expose 20 GiB to the guest while consuming much less host space until the guest writes blocks. Track both the logical capacity and actual allocated host blocks (for example, `ls -l` versus `du`, or the backend's own accounting), and include images, snapshots, retained/deleted volumes, and the storage pool itself in the capacity budget.

Multipass's Linux QEMU driver stores disks as `qcow2`; Multipass's own troubleshooting documentation shows its QEMU command line with `format=qcow2,discard=unmap`. QEMU documents `discard=unmap` as forwarding discard requests to the backing image. This supports thin growth and reclamation when the guest filesystem, virtual disk controller, QEMU, and backing format all pass discard through; it does not mean that the configured maximum disk size is reserved in full at VM creation. [Multipass QEMU command example](https://canonical.com/multipass/docs/latest/how-to-guides/troubleshoot/troubleshoot-launch-start-issues/), [QEMU disk options](https://www.qemu.org/docs/master/system/qemu-manpage.html), [Multipass disk-size controls](https://canonical.com/multipass/docs/latest/how-to-guides/manage-instances/modify-an-instance/)

Incus's `dir` driver stores data as ordinary files and is convenient to inspect, but upstream says it is slower because it must unpack images and perform full copies for instances, snapshots, and images; its operations are not optimized. Incus VM image bundles use a `qcow2` root image, while a running VM volume under `dir` is a raw disk file. Incus converts VM images into the storage driver's representation. Raw VM disk files can be sparse: an upstream Incus issue demonstrates a 20 GiB `root.img` with about 1.6 GiB allocated on a `dir` pool. Sparse behavior must be preserved during copy, export, backup, or restore; the same issue documents export/import paths that produced fully allocated files. [Incus `dir` storage driver](https://linuxcontainers.org/incus/docs/main/reference/storage_dir/), [Incus image format](https://linuxcontainers.org/incus/docs/main/reference/image_format/), [Incus sparse VM volume example and export caveat](https://github.com/lxc/incus/issues/662)

Storage-driver implications:

| Driver | Allocation behavior and operational tradeoff |
|---|---|
| `dir` | Ordinary files on the host filesystem; VM raw files can be sparse. Easy to set up and inspect. Incus warns that operations are slower because image unpacking and instance/snapshot copies are not optimized. Check the underlying filesystem's free space and quota behavior. |
| `lvm` | By default, Incus creates thin logical volumes in an LVM thin pool. Thin volumes can be overcommitted relative to physical capacity, so the pool's data and metadata capacity still need monitoring. If the pool is built on a dedicated VG or device, that capacity is effectively set aside for it; thin provisioning does not create physical storage. Non-thin LVs reserve maximum size and use more space. |
| `zfs` | Uses datasets and zvols, with copy-on-write snapshots/clones. ZFS can reserve space in addition to setting quotas; Incus documentation uses slightly different key spellings in pool and volume contexts (`zfs.use_reserve_space`/`volume.zfs.use_reserve_space` in prose, `zfs.reserve_space` in the volume option table). Confirm the exact option accepted by the installed Incus version before relying on reservation behavior. Snapshots and clones retain referenced blocks; deleting an instance may leave an internally renamed object under `deleted/` while references remain. ZFS also uses RAM for ARC. |
| `btrfs` | Upstream Incus explicitly says not to use VMs with Btrfs pools. VM disks are large files over a copy-on-write filesystem, and random guest writes can cause quota/extent accounting problems and snapshot inefficiency. |

Sources: [Incus LVM driver](https://linuxcontainers.org/incus/docs/main/reference/storage_lvm/), [Incus ZFS driver](https://linuxcontainers.org/incus/docs/main/reference/storage_zfs/), [Incus Btrfs driver](https://linuxcontainers.org/incus/docs/main/reference/storage_btrfs/), [OpenZFS ARC documentation](https://openzfs.github.io/openzfs-docs/Performance%20and%20Tuning/Module%20Parameters.html#zfs-arc-max).

An Incus loop-backed pool has a pool image file with a configured capacity. Current Incus storage-pool documentation describes its default loop-pool size as 20% of free disk space, bounded between 5 GiB and 30 GiB. That is a pool capacity decision distinct from each VM's virtual disk size. Before selecting the pool, check whether the intended setup creates a loop pool or uses a directory on an existing filesystem, and establish where the pool's actual backing space comes from. [Incus storage pools](https://linuxcontainers.org/incus/docs/main/howto/storage_pools/)

### Reclaim experiment before migration

Run this on a disposable VM using the intended Incus storage pool:

1. Create the VM with the same configured disk size expected in production; record pool free space and the host allocation for its disk.
2. Write a known amount of data in the guest; record guest filesystem usage, virtual disk logical size, and host/backend allocated size.
3. Delete a large file in the guest, run filesystem discard (`fstrim`), and repeat the host/backend measurements after sync.
4. Create and delete a snapshot, then measure again. For ZFS, also inspect pool usage and any retained `deleted/` objects; for LVM thin, inspect thin-pool data and metadata usage.
5. Export and restore once using the planned backup path; verify restored host allocation remains sparse/thin where expected.

This distinguishes logical disk limits from physical allocation and catches failures in discard propagation or sparse-file preservation. A `dir` VM may have a sparse raw file and still grow to its full logical size if the guest writes every block or discard is not passed through.

## CPU and memory budgeting

Incus `limits.cpu` on VMs is a **vCPU count/topology**, not exclusive physical core reservation. A numeric count exposes that many vCPUs; by default they are not pinned to dedicated physical cores. VMs start with one vCPU by default. CPU requests should therefore be treated as concurrency/scheduling capacity and load tested alongside other VMs and host services. Incus documents live CPU updates as hotplugging; guests may need an OS-specific action or restart to bring new CPUs online. [Incus instance options: CPU limits](https://linuxcontainers.org/incus/docs/main/reference/instance_options/)

VM `limits.memory` defaults to 1 GiB. Incus can raise VM memory by hotplugging virtual memory sticks, subject to slot and platform limits. Reducing memory uses the guest balloon device and is slow; a requested reduction can fail and need to be retried. Avoid treating a configured limit as a guarantee that changing it downward will immediately reclaim host RAM. Budget for the VM's working set, QEMU overhead, Incus, the host, and storage cache (notably ZFS ARC if selected). [Incus instance options: memory limits and ballooning](https://linuxcontainers.org/incus/docs/main/reference/instance_options/), [Incus ZFS notes](https://linuxcontainers.org/incus/docs/main/reference/storage_zfs/)

For a first capacity pass, count the agent VMs' configured vCPU and RAM limits, but reserve headroom for host services and workload spikes. Do not assume all vCPUs execute simultaneously or all guest RAM is constantly resident; conversely, do not overcommit to the point that simultaneous agent builds or tests cause host memory pressure. Measure a representative peak workload before setting concurrency defaults.

## Shared proxy gateway estimate

If the proxy stays on the host and the Incus VMs use the explicit proxy design in [the egress-enforcement note](lxd-incus-egress-enforcement.md), there is no gateway VM to budget. The existing research concludes that Incus ACLs can allow only the host proxy endpoint and reject other egress, while the proxy runs without firewall privileges.

If transparent proxying requires a dedicated router/proxy VM, treat the following as a prototype starting point, not a measured requirement:

| Resource | Initial allocation | Notes |
|---|---:|---|
| vCPU | 1 | Raise if TLS inspection, high connection concurrency, or encryption throughput saturates it. A vCPU is schedulable capacity, not a dedicated host core. |
| RAM | 512 MiB–1 GiB | Start at 1 GiB if running a general-purpose Ubuntu VM, then measure steady and peak use. This budget excludes memory used by host-side proxy services. |
| Disk | 4–8 GiB logical, sparse/thin | Enough for a minimal gateway OS and packages as an initial estimate. Logs, caches, update retention, or storing the proxy's CA/state may require more. Keep state and logs bounded. |

These numbers are engineering estimates. Benchmark under expected concurrent agent traffic before adopting them. Prefer keeping credentials and proxy policy in the existing host-side proxy when the network design supports that, since moving them into a gateway changes the trust and recovery boundary.

## Installation and operating model

Incus is a system VM/container manager, not a per-user application VM launcher. It requires a host daemon and system networking/storage integration. Installation route depends on the host distribution; upstream documents distribution packages and additional package repositories. After installing, `incus admin init` configures a storage pool and can set up networking. The daemon has administrative control over the host resources it manages, so distinguish trusted host administration from the restricted API identity used by Jetty's provisioner. [Incus installation](https://linuxcontainers.org/incus/docs/main/installing/), [Incus initialization](https://linuxcontainers.org/incus/docs/main/howto/initialize/), [Incus security](https://linuxcontainers.org/incus/docs/main/explanation/security/)

Treat the storage-pool choice as part of the installation decision. A default loop pool is simple, but has a bounded pool capacity; a `dir` pool on an existing filesystem is easy to start with but has slower copy/snapshot operations. LVM thin and ZFS add host-level storage setup and monitoring requirements. Do not create the pool on an assumed-free disk until its source, capacity, and backup behavior are understood.

## VM network devices and isolation

For bridged VM NICs, Incus exposes `security.ipv4_filtering`, `security.ipv6_filtering`, MAC filtering, and `security.port_isolation`. IP filtering prevents the VM from spoofing another source IP and enables MAC filtering; port isolation blocks direct traffic to other isolated NICs on the bridge. These settings constrain what a guest root user can send on its attached NIC, but they do not themselves proxy or deny all egress. Combine them with the network ACL or routing design documented in [lxd-incus-egress-enforcement.md](lxd-incus-egress-enforcement.md). [Incus NIC configuration options](https://linuxcontainers.org/incus/docs/main/reference/devices_nic/), [Incus network ACLs](https://linuxcontainers.org/incus/docs/main/howto/network_acls/)

The VM should receive only the intended managed NIC through a profile. Avoid giving an agent authority to attach arbitrary unmanaged NICs or alter the shared network definition. For the explicit host-proxy design, verify both IPv4 and IPv6 behavior and keep the host proxy port reachable from the VM bridge while default-denying other egress. If choosing a gateway VM, make the gateway the only routed exit and verify from a guest with root access that adding routes, addresses, or proxy environment overrides cannot bypass it.

## Decision to carry into the migration plan

For an incremental trial, use one disposable VM and a `dir` pool on a filesystem with ample free space, while measuring logical versus allocated disk usage and clone/snapshot latency. This makes behavior visible and avoids committing to ZFS or LVM setup before the workload is characterized. If snapshot/copy speed or dense VM count becomes a bottleneck, compare LVM thin and ZFS with the same reclaim and lifecycle experiment. Do not use Btrfs for the VM pool. Keep the host-side proxy if Incus's explicit-proxy plus ACL design preserves the required proxy capabilities; a gateway VM is a separate architectural choice, not a prerequisite for Incus.
