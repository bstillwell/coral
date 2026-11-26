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

`get_cluster_state()` finishes by calling `calculate_pg_distribution()`, which fills in `osds[osd_id]['target_pgs_by_pool'][pool_id] = (floor, ceil)` — the per-OSD min/max PG count for a balanced cluster, derived from the OSD's CRUSH-weight share among the pool rule's `valid_osds`, scaled by `pg_num * pool_size` (replica count or k+m). OSDs whose `crush_weight < 0.0001` are treated as zero so they don't pull a share away from the rest. `capped_ideals()` caps each failure domain (the rule's, from `osd_bucket_maps`) at `pg_num` shards, one per PG, sharing the excess among the other domains by weight until none is over; without it, OSDs in an oversized domain get targets they can never reach.

`calculate_usage()` populates `current_usage`/`future_usage` and per-pool PG counts (`current_pgs_by_pool` / `future_pgs_by_pool`) per OSD. `rebuild_upmaps()` then simulates removing every upmap: undoing each PG's `pg_upmap_items` last-first (the reverse of how Ceph applies them — an item applies only if `from` is in the set, `to` isn't, and `to` isn't out) recovers the placement CRUSH computes with no upmaps. It then re-adds an existing item only if it's *helpful* — moves a PG from an OSD over its target toward one under it (same test as below) — and wouldn't leave an OSD with upmaps moving data both onto and off it in the same pool (`would_mix_roles()`, backed by per-(pool, OSD) counts in `cluster_state['upmap_roles']`). Dropped items send their PGs back to CRUSH's placement. After that, `balance_off_target_osds()` walks the pools largest average shard first (`pool_shard_bytes()`) and queues new upmaps — via `queue_upmap_mapping_addition()` — that move PGs from donor OSDs (count > floor) to recipients (count < ceil), committing a move only if the donor is over its ceil or the recipient is under its floor, while respecting the pool's CRUSH rule (`valid_osds`), failure-domain uniqueness (looked up via `osd_bucket_maps[failure_domain]`) and `would_mix_roles()`. It tries moves back onto an OSD that already holds the PG's shard (`pg['original_up']`) first, since those cost no data movement. For speed it keeps a per-pool OSD → PG index (so a donor only scans its own PGs), skips donors with no valid recipients, groups recipients by failure domain, and pre-checks roles before calling `would_mix_roles()`; these shortcuts only skip candidates the full checks would reject, so results are identical to a plain scan — keep it that way when changing the checks. Donors and recipients with equal room are ordered by projected utilization (fullest gives, emptiest receives), so each pool's extra PGs (ceil) land on OSDs left emptiest by the larger pools. Once a pool's counts are in range, `balance_pool_bytes()` moves shards between in-range OSDs from fuller to emptier, through the same `move_one()` checks plus an `accept` test: utilization gap at least `BYTE_BALANCE_MIN_GAP`, shard at least `BYTE_BALANCE_MIN_SHARD` of the receiver's size, and the receiver must not end up fuller than the donor (so it can't oscillate). When no such move exists, `drain_to_at_ceil_osd()` lets an OSD with a target of `(0, 0)` (out, or CRUSH weight ~0) hand a PG to an OSD already at its ceil; that OSD is then over-ceil and passes a different PG on during the next pass. This drain path is the one exception to `would_mix_roles()` (the relay OSD is tracked in `cluster_state['drain_relays']`). `plan_upmaps()` wraps those two steps and returns the plan — `{pgid: target up set}` from `build_upmap_plan()` for PGs whose items changed — plus a `plan_fingerprint()` of the OSD map (CRUSH version, OSD up/in/reweight, pool pg_num/pgp_num (`pg_placement_num`)/size/rule; everything but upmaps). For replicated pools `build_upmap_plan()` reorders each target with `align_replicas()` so OSDs that stay in the PG keep their slot — replicas are interchangeable, and slot-by-slot stepping would otherwise move a staying OSD's copy off and back on.

Changes are applied one shard per PG at a time, because all of a PG's backfill flows through its primary and the PG holds backfill reservations on the primary and every target until the whole PG finishes. `run_upmap_plan()` loops every `--interval` seconds: it re-fetches `osd dump` (returning `'replan'` if the fingerprint changed) and `pg dump`, then `apply_upmap_round()` gives every PG that isn't at its target and is settled (`is_pg_settled()`: active+clean, not remapped, up == acting) the items for its next single-shard step. `next_upmap_step()` recovers CRUSH's raw set with `unapply_upmap_items()`, changes one position of the up set, and `items_for_up()` builds the minimal `pg_upmap_items` for it, ordered so each item applies (checked with `apply_upmap_items()`, a port of `OSDMap::_apply_upmap`). Every intermediate up set must keep the pool rule's failure domains distinct (`failure_domain_conflicts()`, with per-pool bucket maps from `pool_failure_domains()`): otherwise the mon's `OSDMap::check_pg_upmaps` → `CrushWrapper::verify_upmap` cancels *all* of the PG's upmap items. So `next_upmap_step()` tries single-shard steps first (fullest first) and skips invalid ones. If none is valid, shards are trading failure domains in a cycle, and `hop_step()` moves one of them to a temporary OSD in a failure domain the PG neither uses nor wants. That frees its domain so the rest of the cycle can step one shard at a time, and the hopping shard reaches its target last (one extra shard move). Hop candidates are the pool rule's up+in OSDs that aren't nearfull (`spare_osds`, from `pool_rule_osds()`), least-used first, spread across OSDs within a round. A hop is only taken if a single-shard step follows it, so every hop is followed by progress. Without a usable spare it changes the fewest shards together that is valid. A PG with no valid step at all gets its whole move in one change. `send_upmap_items()` sends the command and checks `mon_command`'s return code.

PG splits are handled before any balancing. `splitting_pools()` finds pools whose `pg_num` or `pgp_num` (`pg_placement_num`) is below its target (merges, with targets below current values, are excluded: they need PGs co-located). The mgr (`DaemonServer::adjust_pgs`) raises `pgp_num` only while the cluster-wide misplaced ratio is below `target_max_misplaced_ratio`, so in `main()` coral calls `announce_splits()` (prints a note on stdout, even with `--preview`) and, with `--apply`, `wait_out_splits()` → `hold_splits()`: every `--interval` seconds `pin_remapped_pgs()` gives each remapped PG of a splitting pool the items from `pin_items()` that make its up set match its acting set (like upmap-remapped; replicated pools match membership only via `align_replicas()`; skipped if degraded, not active, an acting OSD is out, or the pins would put two OSDs in one failure domain). Once no pool is splitting it re-reads the cluster state and plans as usual; the rebuild then drops pins that don't help balance. A split starting mid-run changes the fingerprint, and the re-plan loop in `main()` holds it the same way. The dry run prints the pins (`print_split_holds()`) instead of a plan.

Full OSDs are handled first. Utilization (`osd_utilization()`, `kb_used / kb` from `pg dump` `osd_stats`) is re-read every round; `next_upmap_step()` moves the shard off the fullest OSD first. In `apply_upmap_round()` a PG is *urgent* if its plan moves a shard off an OSD at or above `nearfull_ratio`; while any urgent PG can step or is progressing (backfilling / recovering or queued, not toofull), only urgent PGs and no-data-movement steps start, fullest first. Urgency is per PG, not per step, so an urgent PG whose next step has to move a different shard first still goes, and a stuck urgent PG doesn't stall the others. In planning, `fits_below_backfillfull()` (projected usage = raw `used` + `future_usage` − `current_usage`, or `future_usage` if `used` wasn't reported) gates every added move in `rebuild_upmaps()`, `balance_off_target_osds()` and `drain_to_at_ceil_osd()`, and `rebuild_upmaps()` restores dropped upmaps when sending their PGs back would put the source OSD past `backfillfull_ratio`.

## Pre-flight checks

`main()` connects and calls `run_coral()`, turning `CephCommandError` / `PreflightError` into an error message and exit status 1. `run_coral()` starts with `preflight_checks(cluster, osd_dump)` → `(errors, warnings)`, printed by `print_preflight()`. Errors (mgr balancer on, via `balancer_active()` / `mgr_command('balancer status')`; `require_min_compat_client` before luminous) raise `PreflightError` under `--apply`; a dry run continues. Warnings: `norebalance` / `nobackfill` (`BACKFILL_BLOCKING_FLAGS`, from `osd_flags()`), `norecover`, unknown balancer state. Each round of `run_upmap_plan()` and `hold_splits()` calls `check_balancer_off()`; `run_upmap_plan()` also starts nothing while a blocking flag is set. Both retry a round that raised `CephCommandError`, up to `MAX_FAILED_ROUNDS` in a row.

## RADOS Communication

Ceph queries go through `mon_json(cluster, prefix, **args)`, which runs `cluster.mon_command()` and raises `CephCommandError` on a non-zero return code or output that isn't JSON:

```python
osd_dump = mon_json(cluster, 'osd dump')
crush_tree = mon_json(cluster, 'osd crush tree', shadow='--show-shadow')
```

`send_upmap_items()` sends the upmap changes and checks the return code itself (a refused change is logged and skipped). The only mgr command is `balancer status`. The `timeout` argument to `mon_command` is ignored by current rados bindings.

Dry-run mode (`print_upmap_plan()`) runs one `apply_upmap_round()` against the starting state with `is_dryrun=True`, which prints the equivalent `ceph` CLI commands instead of sending them, followed by a summary of the remaining PGs and shard moves.

## Output

The tables in `display_usage()` / `display_pgs_by_pool()` print through `print_table_line()`, which swaps the box-drawing characters for ASCII (`ASCII_BORDERS`) unless stdout is a terminal, so they survive `watch` builds that drop non-ASCII bytes. Keep other printed output ASCII too.

## Usage Calculation

- **Replica pools**: each OSD in the acting set is credited the full `num_bytes` of the PG.
- **Erasure-coded pools**: each OSD is credited `num_bytes / k` (k = number of data shards from the EC profile).

`current_usage` is derived from the acting set; `future_usage` from the up set (where data will land after backfill completes). Off-target detection operates on `future_pgs_by_pool` (PG counts on the up set) compared against `target_pgs_by_pool`.
