# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Coral (Clyso Optimized RebALancer) is a Ceph cluster rebalancing tool. It analyzes cluster state, detects OSDs whose future usage would exceed the `backfillfull_ratio`, and removes pg_upmap_items that cause the overloading.

## Running

```bash
# Normal mode — connects to Ceph, optimizes, applies changes, logs to /var/log/ceph/coral.log
./coral.py

# Preview — show current/future OSD usage without modifying anything
./coral.py --preview

# Dry-run — print the Ceph commands that would be executed
./coral.py --dry-run
```

Requires the `rados` Python package (Ceph RADOS bindings) and a reachable Ceph cluster at `/etc/ceph/ceph.conf`.

## Architecture

All state lives in a single `cluster_state` dict built by `get_cluster_state()`, which calls:

```
get_osd_info()        → osds: up/in/crush_weight/size/device_class
get_pg_info()         → pgs: up/acting sets, num_bytes, existing upmaps
get_pool_info()       → pools: type (replica|erasure), crush_rule, k/m for EC
get_crush_rules()     → crush_rules: rule steps
get_osd_bucket_maps() → osd_bucket_maps: failure-domain → OSD → bucket
get_backfillfull_ratio() → backfillfull_ratio float
```

After `calculate_usage()` populates `current_usage`/`future_usage` per OSD, `remove_overfull_mappings()` iteratively finds OSDs where `future_usage/size >= backfillfull_ratio` and queues removal of upmap items redirecting data to those OSDs via `queue_upmap_mapping_removal()`. Finally `apply_upmap_queue()` sends `osd pg-upmap-items` / `osd rm-pg-upmap-items` commands to the cluster.

## RADOS Communication

All Ceph queries and mutations use `cluster.mon_command()`:

```python
cmd = {'prefix': 'osd dump', 'format': 'json'}
ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
```

Dry-run mode intercepts `apply_upmap_queue()` and prints the equivalent `ceph` CLI commands instead.

## Usage Calculation

- **Replica pools**: each OSD in the acting set is credited the full `num_bytes` of the PG.
- **Erasure-coded pools**: each OSD is credited `num_bytes / k` (k = number of data shards from the EC profile).

`current_usage` is derived from the acting set; `future_usage` from the up set (where data will land after backfill completes). Overfull detection operates on `future_usage`.
