# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Coral (Clyso Optimized RebALancer) is a Ceph cluster rebalancing tool. It analyzes cluster state, compares each OSD's projected PG count against its per-pool target range, and removes pg_upmap_items that push OSDs outside that range.

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

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/ -v
```

No Ceph cluster required — `rados` is mocked via `sys.modules` in `tests/conftest.py` before `coral` is imported.

## Architecture

All state lives in a single `cluster_state` dict built by `get_cluster_state()`, which calls:

```
get_osd_info()        → osds: up/in/crush_weight/size/device_class
get_pg_info()         → pgs: up/acting sets, num_bytes, existing upmaps
get_pool_info()       → pools: type (replica|erasure), crush_rule, k/m for EC
get_crush_rules()     → crush_rules: rule steps, valid_osds (OSDs the rule can target, resolved via the recursive get_osds_in_bucket() helper)
get_osd_bucket_maps() → osd_bucket_maps: failure-domain → OSD → bucket
get_backfillfull_ratio() → backfillfull_ratio float
```

`get_cluster_state()` finishes by calling `calculate_pg_distribution()`, which fills in `osds[osd_id]['target_pgs_by_pool'][pool_id] = (floor, ceil)` — the per-OSD min/max PG count for a balanced cluster, derived from the OSD's CRUSH-weight share among the pool rule's `valid_osds`, scaled by `pg_num * pool_size` (replica count or k+m). OSDs whose `crush_weight < 0.0001` are treated as zero so they don't pull a share away from the rest.

`calculate_usage()` populates `current_usage`/`future_usage` and per-pool PG counts (`current_pgs_by_pool` / `future_pgs_by_pool`) per OSD. `balance_off_target_osds()` then walks each pool and queues new upmaps — via `queue_upmap_mapping_addition()` — that move PGs from the most over-ceil OSD to the most under-floor OSD, while respecting the pool's CRUSH rule (`valid_osds`) and failure-domain uniqueness (looked up via `osd_bucket_maps[failure_domain]`). Finally `apply_upmap_queue()` sends `osd pg-upmap-items` / `osd rm-pg-upmap-items` commands to the cluster.

`remove_off_target_mappings()` (the inverse — removing existing upmaps that push OSDs off-target) exists but is currently disabled in `main()` pending a rewrite focused on reducing the total upmap mapping count.

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

`current_usage` is derived from the acting set; `future_usage` from the up set (where data will land after backfill completes). Off-target detection operates on `future_pgs_by_pool` (PG counts on the up set) compared against `target_pgs_by_pool`.
