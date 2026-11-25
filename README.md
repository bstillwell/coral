# Coral

Coral (Clyso Optimized RebALancer) is a Ceph cluster rebalancing tool. It analyzes current and future PG placement, compares each OSD's projected PG count against its per-pool target range, and adds pg_upmap_items that move PGs from over-target OSDs to under-target ones — honoring each pool's CRUSH rule and failure-domain constraints.

## Requirements

- Python 3
- `rados` Python bindings (part of the Ceph client libraries)
- Access to a Ceph cluster via `/etc/ceph/ceph.conf`

## Usage

```
./coral.py [--preview] [--apply [--interval SECONDS]] [--preview-pgs]
```

| Flag | Behavior |
|------|----------|
| _(none)_ | Default: dry-run. Connect to the cluster, compute the changes, print the first round of `ceph osd pg-upmap-items` / `ceph osd rm-pg-upmap-items` commands that `--apply` would send, and summarize how many PGs and shard moves remain. Logs to `/var/log/ceph/coral.log`. |
| `--apply` | Push the changes to the cluster one shard per PG at a time. Every `--interval` seconds, each PG that is active+clean and not yet at its target gets its next single-shard change. Runs until every PG is at its target; re-plans if OSDs, weights, pools or the CRUSH map change; Ctrl-C stops it between rounds. |
| `--interval` | Seconds between rounds with `--apply` (default 60). |
| `--preview` | Show current and future OSD usage based on existing PG up/acting sets only — no rebalancing computation, no cluster writes, no log file writes. |
| `--preview-pgs` | After the usage table, also print a per-OSD breakdown of future PGs by pool with their target floor/ceil. |

## How it works

1. **Gather cluster state** — OSD capacity and status, PG placement (acting and up sets), pool metadata, CRUSH rules (including the set of OSDs each rule can target), and the cluster `backfillfull_ratio`. Per-OSD min/max PG targets for each pool are also derived from each OSD's CRUSH-weight share among the rule's eligible OSDs, scaled by `pg_num` and pool size.
2. **Calculate usage** — for each OSD, sum the data it holds today (acting set) and where it will land after backfill completes (up set). Erasure-coded pools distribute `num_bytes / k` per OSD; replica pools distribute the full `num_bytes`.
3. **Rebuild existing upmaps** — undo every existing `pg_upmap_items` entry to recover the placement CRUSH computes on its own, then keep only the entries that move a PG from an over-target OSD toward an under-target one. No OSD is left with upmaps moving data both onto and off it in the same pool.
4. **Balance off-target OSDs** — for each pool, the most over-target OSD has one of its PGs redirected (via a new `pg-upmap-items` entry) to the most under-target OSD that the pool's CRUSH rule allows and that doesn't collide on failure domain with the PG's other replicas. Repeats until no valid moves remain for the pool.
5. **Display results** — a color-coded table shows each OSD's size, current usage, future usage, and the delta.
6. **Apply changes** — one shard per PG at a time. All of a PG's backfill flows through its primary OSD, and the PG holds a backfill reservation on the primary and every target OSD until the whole PG is done, so moving every shard of a wide EC PG at once is slow and blocks other PGs on those OSDs. Instead, each round sends every settled PG the upmap items for its next single-shard step. Every step keeps the PG's failure domains distinct — the mon cancels all of a PG's upmap items if they put two OSDs in one failure domain — so when no single-shard step is valid (e.g. two shards trading failure domains), coral changes the fewest shards together that is.

Full OSDs come first. Each PG's next step moves the shard off its fullest OSD, and while any PG with a planned move off a nearfull OSD (at or above the cluster's `nearfull_ratio`) can step or is backfilling, only those PGs start — the rest wait. Planning never adds a move that would take an OSD past `backfillfull_ratio` (Ceph would hold it as `backfill_toofull`), and keeps an existing upmap if dropping it would send data back onto an OSD past that ratio.

## Output

```
OSD  | Class | Weight   | Size        | Current Usage              | Future Usage               | Change
-----+-------+----------+-------------+----------------------------+----------------------------+----------------------------
0    | hdd   |  1.00000 |  3726.0 GiB |  800.3 GiB   21.5%   42 PGs |  850.1 GiB   22.8%   44 PGs |  +49.8 GiB    +1.3%   +2 PGs
```

Usage percentage is color-coded: green (>0%), yellow (>80%), red (>90%), bright red (>100%).
