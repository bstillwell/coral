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
| `--apply` | Push the changes to the cluster one shard per PG at a time. Every `--interval` seconds, each PG that is active+clean and not yet at its target gets its next single-shard change. Runs until every PG is at its target; re-plans if OSDs, weights, pools or the CRUSH map change; Ctrl-C stops it between rounds. If a pool is splitting PGs, first holds its data in place until the split finishes (see below). |
| `--interval` | Seconds between rounds with `--apply` (default 60). |
| `--preview` | Show current and future OSD usage based on existing PG up/acting sets only — no rebalancing computation, no cluster writes, no log file writes. |
| `--preview-pgs` | After the usage table, also print a per-OSD breakdown of future PGs by pool with their target floor/ceil. |

## Pre-flight checks

Before anything else, coral checks the cluster and prints what it finds, in every mode:

- **Errors** stop `--apply` before it changes anything (a dry run still prints its plan):
  - the mgr balancer is on (`ceph balancer off`): it would fight coral's changes;
  - `require_min_compat_client` is older than luminous: the mons reject upmap commands.
- **Warnings**:
  - `norebalance` or `nobackfill` is set: no data moves, so with `--apply` coral starts no new changes until the flag is unset (it keeps checking every round);
  - `norecover` is set: degraded PGs won't recover, and coral only steps a PG once it's active+clean;
  - the balancer's state couldn't be read.

While running, coral stops if the balancer is turned on, and retries a round if a query to the mons fails (giving up after 5 failed rounds in a row). Any failed query exits with an error instead of a traceback.

## How it works

1. **Gather cluster state** — OSD capacity and status, PG placement (acting and up sets), pool metadata, CRUSH rules (including the set of OSDs each rule can target), and the cluster `backfillfull_ratio`. Per-OSD min/max PG targets for each pool are also derived from each OSD's CRUSH-weight share among the rule's eligible OSDs, scaled by `pg_num` and pool size. A failure domain can hold at most one shard of each PG, so a domain whose weight share asks for more (e.g. a cage with more than 1/7 of the weight under an EC 4+3 rule) is capped at `pg_num` shards, and the rest is shared among the other domains by weight.
2. **Calculate usage** — for each OSD, sum the data it holds today (acting set) and where it will land after backfill completes (up set). Erasure-coded pools distribute `num_bytes / k` per OSD; replica pools distribute the full `num_bytes`.
3. **Rebuild existing upmaps** — undo every existing `pg_upmap_items` entry to recover the placement CRUSH computes on its own, then keep only the entries that move a PG from an over-target OSD toward an under-target one. No OSD is left with upmaps moving data both onto and off it in the same pool.
4. **Balance off-target OSDs** — pools are balanced largest shards first. For each pool, the most over-target OSD has one of its PGs redirected (via a new `pg-upmap-items` entry) to the most under-target OSD that the pool's CRUSH rule allows and that doesn't collide on failure domain with the PG's other replicas. Repeats until no valid moves remain for the pool.
5. **Balance bytes** — each pool's range is only floor..floor+1 PGs per OSD, and which OSDs get the extra PG is free to choose. Coral gives it to the OSDs that are emptiest in bytes so far (projected utilization), so smaller pools fill in around the larger ones: among equally good moves, the fullest OSD gives and the emptiest receives. Then a byte pass moves shards from fuller to emptier OSDs while both stay within their range, if their utilization differs by at least 1 percentage point, the shard is at least 0.1% of the receiver's size, and the receiver doesn't end up fuller than the donor.
6. **Display results** — a color-coded table shows each OSD's size, current usage, future usage, and the delta.
7. **Apply changes** — one shard per PG at a time. All of a PG's backfill flows through its primary OSD, and the PG holds a backfill reservation on the primary and every target OSD until the whole PG is done, so moving every shard of a wide EC PG at once is slow and blocks other PGs on those OSDs. Instead, each round sends every settled PG the upmap items for its next single-shard step. Every step keeps the PG's failure domains distinct — the mon cancels all of a PG's upmap items if they put two OSDs in one failure domain — so when shards have to trade failure domains (shard 1 moving into the domain shard 2 is leaving, and so on round a cycle) and no single shard can move alone, one of them first hops to a temporary OSD in a failure domain the PG doesn't use or want. The rest then move one at a time and the hopping shard goes to its target last, which costs one extra shard move. Only if no such OSD exists does coral change the fewest shards together that is valid.

**PG splits.** When a pool is splitting (its `pg_num` or `pgp_num` is still growing toward its target), coral says so and, with `--apply`, holds that data in place before balancing anything. Each `pgp_num` step sends child PGs to their own CRUSH placement, and the mgr only takes the next step while less than `target_max_misplaced_ratio` (cluster-wide) of the data is misplaced, so a split normally waits on backfill after every step. Like [upmap-remapped](https://github.com/cernceph/ceph-scripts/blob/master/tools/upmap/upmap-remapped.py), coral pins every remapped PG of a splitting pool to the OSDs that hold its data now (upmap items that make its up set match its acting set), every `--interval` seconds, so the mgr can keep raising `pgp_num` straight away. Once no pool is splitting, coral plans and applies the balance as usual, which moves the data to its final place one shard at a time. Merges are left alone: they need the PGs to move together. The dry run prints the pins it would set instead of a balance plan.

Full OSDs come first. Each PG's next step moves the shard off its fullest OSD, and while any PG with a planned move off a nearfull OSD (at or above the cluster's `nearfull_ratio`) can step or is backfilling, only those PGs start — the rest wait. Planning never adds a move that would take an OSD past `backfillfull_ratio` (Ceph would hold it as `backfill_toofull`), and keeps an existing upmap if dropping it would send data back onto an OSD past that ratio.

## Output

```
OSD  | Class | Weight   | Size        | Current Usage              | Future Usage               | Change
-----+-------+----------+-------------+----------------------------+----------------------------+----------------------------
0    | hdd   |  1.00000 |  3726.0 GiB |  800.3 GiB   21.5%   42 PGs |  850.1 GiB   22.8%   44 PGs |  +49.8 GiB    +1.3%   +2 PGs
```

Usage percentage is color-coded: green (>0%), yellow (>80%), red (>90%), bright red (>100%).

On a terminal the tables use box-drawing characters. When the output goes anywhere else (`watch`, a pipe, a file), they use plain ASCII (`+`, `-`, `|`) instead: some builds of procps `watch`, e.g. 3.3.17 without its UTF-8 support, drop every non-ASCII byte, which would erase the borders. `watch --color ./coral.py --preview` keeps the colors.
