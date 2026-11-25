# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Coral (Clyso Optimized RebALancer) is a Ceph cluster rebalancing tool. It analyzes cluster state, compares each OSD's projected PG count against its per-pool target range, and removes pg_upmap_items that push OSDs outside that range.

## Running

```bash
# Default (dry-run) — connects to Ceph, computes upmap changes, prints the
# first round of pg-upmap-items commands instead of applying them. Safe to run.
./coral.py

# Preview — show current/future OSD usage without modifying anything and
# without writing to the log file
./coral.py --preview

# Apply — step every PG toward its target one shard at a time, a round every
# --interval seconds (default 60), until done
./coral.py --apply
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
get_pg_dump()         → raw 'pg dump' (the largest query), fetched once and passed to the next two
get_osd_info()        → osds: up/in (osd dump), crush_weight/device_class (crush tree), size (pg dump osd_stats)
get_pg_info()         → pgs: up/acting sets, num_bytes, existing upmaps
get_pool_info()       → pools: type (replica|erasure), crush_rule, k/m for EC
get_crush_rules()     → crush_rules: rule steps, valid_osds (OSDs the rule can target, resolved via the recursive get_osds_in_bucket() helper)
get_osd_bucket_maps() → osd_bucket_maps: failure-domain → OSD → bucket
get_backfillfull_ratio() → backfillfull_ratio float
```

`get_cluster_state()` finishes by calling `calculate_pg_distribution()`, which fills in `osds[osd_id]['target_pgs_by_pool'][pool_id] = (floor, ceil)` — the per-OSD min/max PG count for a balanced cluster, derived from the OSD's CRUSH-weight share among the pool rule's `valid_osds`, scaled by `pg_num * pool_size` (replica count or k+m). OSDs whose `crush_weight < 0.0001` are treated as zero so they don't pull a share away from the rest.

`calculate_usage()` populates `current_usage`/`future_usage` and per-pool PG counts (`current_pgs_by_pool` / `future_pgs_by_pool`) per OSD. `rebuild_upmaps()` then simulates removing every upmap: undoing each PG's `pg_upmap_items` last-first (the reverse of how Ceph applies them — an item applies only if `from` is in the set, `to` isn't, and `to` isn't out) recovers the placement CRUSH computes with no upmaps. It then re-adds an existing item only if it's *helpful* — moves a PG from an OSD over its target toward one under it (same test as below) — and wouldn't leave an OSD with upmaps moving data both onto and off it in the same pool (`would_mix_roles()`, backed by per-(pool, OSD) counts in `cluster_state['upmap_roles']`). Dropped items send their PGs back to CRUSH's placement. After that, `balance_off_target_osds()` walks each pool and queues new upmaps — via `queue_upmap_mapping_addition()` — that move PGs from donor OSDs (count > floor) to recipients (count < ceil), committing a move only if the donor is over its ceil or the recipient is under its floor, while respecting the pool's CRUSH rule (`valid_osds`), failure-domain uniqueness (looked up via `osd_bucket_maps[failure_domain]`) and `would_mix_roles()`. It tries moves back onto an OSD that already holds the PG's shard (`pg['original_up']`) first, since those cost no data movement. When no such move exists, `drain_to_at_ceil_osd()` lets an OSD with a target of `(0, 0)` (out, or CRUSH weight ~0) hand a PG to an OSD already at its ceil; that OSD is then over-ceil and passes a different PG on during the next pass. This drain path is the one exception to `would_mix_roles()` (the relay OSD is tracked in `cluster_state['drain_relays']`). `plan_upmaps()` wraps those two steps and returns the plan — `{pgid: target up set}` from `build_upmap_plan()` for PGs whose items changed — plus a `plan_fingerprint()` of the OSD map (CRUSH version, OSD up/in/reweight, pool pg_num/size/rule; everything but upmaps).

Changes are applied one shard per PG at a time, because all of a PG's backfill flows through its primary and the PG holds backfill reservations on the primary and every target until the whole PG finishes. `run_upmap_plan()` loops every `--interval` seconds: it re-fetches `osd dump` (returning `'replan'` if the fingerprint changed) and `pg dump`, then `apply_upmap_round()` gives every PG that isn't at its target and is settled (`is_pg_settled()`: active+clean, not remapped, up == acting) the items for its next single-shard step. `next_upmap_step()` recovers CRUSH's raw set with `unapply_upmap_items()`, changes one position of the up set, and `items_for_up()` builds the minimal `pg_upmap_items` for it, ordered so each item applies (checked with `apply_upmap_items()`, a port of `OSDMap::_apply_upmap`). A PG with no single-shard step (an EC position swap) gets its whole move in one change. `send_upmap_items()` sends the command and checks `mon_command`'s return code.

## RADOS Communication

All Ceph queries and mutations use `cluster.mon_command()`:

```python
cmd = {'prefix': 'osd dump', 'format': 'json'}
ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
```

Dry-run mode (`print_upmap_plan()`) runs one `apply_upmap_round()` against the starting state with `is_dryrun=True`, which prints the equivalent `ceph` CLI commands instead of sending them, followed by a summary of the remaining PGs and shard moves.

## Usage Calculation

- **Replica pools**: each OSD in the acting set is credited the full `num_bytes` of the PG.
- **Erasure-coded pools**: each OSD is credited `num_bytes / k` (k = number of data shards from the EC profile).

`current_usage` is derived from the acting set; `future_usage` from the up set (where data will land after backfill completes). Off-target detection operates on `future_pgs_by_pool` (PG counts on the up set) compared against `target_pgs_by_pool`.
