# Coral

Coral (Clyso Optimized RebALancer) is a Ceph cluster rebalancing tool. It analyzes current and future PG placement, compares each OSD's projected PG count against its per-pool target range, and adds pg_upmap_items that move PGs from over-target OSDs to under-target ones — honoring each pool's CRUSH rule and failure-domain constraints.

## Requirements

- Python 3
- `rados` Python bindings (part of the Ceph client libraries)
- Access to a Ceph cluster via `/etc/ceph/ceph.conf`

## Usage

```
./coral.py [--preview] [--apply] [--preview-pgs]
```

| Flag | Behavior |
|------|----------|
| _(none)_ | Default: dry-run. Connect to the cluster, compute the changes, and print the `ceph osd pg-upmap-items` / `ceph osd rm-pg-upmap-items` commands that would be run. Logs to `/var/log/ceph/coral.log`. |
| `--apply` | Actually push the upmap changes to the cluster. |
| `--preview` | Show current and future OSD usage without modifying anything and without writing to the log file. Implies dry-run. |
| `--preview-pgs` | After the usage table, also print a per-OSD breakdown of future PGs by pool with their target floor/ceil. |

## How it works

1. **Gather cluster state** — OSD capacity and status, PG placement (acting and up sets), pool metadata, CRUSH rules (including the set of OSDs each rule can target), and the cluster `backfillfull_ratio`. Per-OSD min/max PG targets for each pool are also derived from each OSD's CRUSH-weight share among the rule's eligible OSDs, scaled by `pg_num` and pool size.
2. **Calculate usage** — for each OSD, sum the data it holds today (acting set) and where it will land after backfill completes (up set). Erasure-coded pools distribute `num_bytes / k` per OSD; replica pools distribute the full `num_bytes`.
3. **Balance off-target OSDs** — for each pool, the most over-target OSD has one of its PGs redirected (via a new `pg-upmap-items` entry) to the most under-target OSD that the pool's CRUSH rule allows and that doesn't collide on failure domain with the PG's other replicas. Repeats until no valid moves remain for the pool.
4. **Apply changes** — the queued upmap modifications are sent to the MON via RADOS.
5. **Display results** — a color-coded table shows each OSD's size, current usage, future usage, and the delta.

## Output

```
OSD  | Class | Weight   | Size        | Current Usage              | Future Usage               | Change
-----+-------+----------+-------------+----------------------------+----------------------------+----------------------------
0    | hdd   |  1.00000 |  3726.0 GiB |  800.3 GiB   21.5%   42 PGs |  850.1 GiB   22.8%   44 PGs |  +49.8 GiB    +1.3%   +2 PGs
```

Usage percentage is color-coded: green (>0%), yellow (>80%), red (>90%), bright red (>100%).
