#!/usr/bin/env python3

import argparse
import itertools
import json
import logging
import math
import rados
import sys
import time
from collections import defaultdict

BLUE = '\033[0;34m'
CYAN = '\033[0;36m'
GREEN = '\033[0;32m'
RED = '\033[0;31m'
L_RED = '\033[1;31m'
YELLOW = '\033[0;33m'
RESET = '\033[0m'

CRUSH_ITEM_NONE = 0x7fffffff

def main():
    parser = argparse.ArgumentParser(description="Coral (Clyso Optimized RebALancer)")
    parser.add_argument('--preview', action='store_true',
                        help="Show the current and future cluster state based on existing PG up/acting sets only — no rebalancing computation, no cluster or log writes")
    parser.add_argument('--apply', action='store_true',
                        help="Apply the upmap changes to the cluster, one shard per PG at a time: every --interval seconds, each PG that is active+clean and not yet at its target gets its next single-shard change. Runs until every PG is at its target. Without this flag, coral runs in dry-run mode and prints the first round of pg-upmap-items commands instead.")
    parser.add_argument('--interval', type=float, default=60,
                        help="Seconds between rounds when applying (default: 60)")
    parser.add_argument('--preview-pgs', action='store_true',
                        help="After the usage table, print a per-OSD breakdown of future PGs by pool with their target floor/ceil")
    args = parser.parse_args()

    is_preview = args.preview
    is_dryrun = is_preview or not args.apply

    logger = setup_logging("/var/log/ceph/coral.log", is_preview=args.preview)
    logger.info("Initializing Coral Balancer")

    # Connect to the cluster
    logger.debug("Connecting to Ceph cluster")
    cluster = rados.Rados(conffile="/etc/ceph/ceph.conf")
    cluster.connect()

    # Get the current state of the cluster
    cluster_state = get_cluster_state(cluster, logger)

    # Calculate future OSD usage based on the 'up' set
    calculate_usage(cluster_state, logger)

    # In preview mode, skip the rebalancing computation entirely so the
    # displayed current/future state reflects only what the cluster will do on
    # its own from existing PG up/acting sets.
    if not is_preview:
        # Work out the target placement for every PG
        plan, fingerprint = plan_upmaps(cluster, cluster_state, logger)

    # Provide a nice display of current and future OSD usage
    display_usage(cluster_state)

    if args.preview_pgs:
        display_pgs_by_pool(cluster_state)

    if not is_preview:
        if is_dryrun:
            print_upmap_plan(cluster_state, plan, args.interval, logger)
        else:
            try:
                # Step every PG toward its target one shard at a time,
                # re-planning if the cluster changes underneath us
                while run_upmap_plan(cluster, plan, fingerprint, args.interval, logger,
                                     failure_domains=pool_failure_domains(cluster_state),
                                     pool_osds=pool_rule_osds(cluster_state)) == 'replan':
                    cluster_state = get_cluster_state(cluster, logger)
                    calculate_usage(cluster_state, logger)
                    plan, fingerprint = plan_upmaps(cluster, cluster_state, logger)
            except KeyboardInterrupt:
                logger.info("Interrupted; stopping between rounds. Re-run to continue.")

    # Disconnect from the cluster
    logger.debug("Disconnecting from Ceph cluster")
    cluster.shutdown()

def setup_logging(log_file, is_preview=False):
    logger = logging.getLogger('Coral')
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        # Suppress logging output in preview mode
        if is_preview:
            logger.addHandler(logging.NullHandler())
            return logger

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        # File handler
        try:
            fh = logging.FileHandler(log_file)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        except PermissionError:
            print(f"{YELLOW}Warning: Could not open {log_file} for writing.  File logging disabled.{RESET}")

        # Console handler
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

    return logger

def get_pg_dump(cluster):
    cmd = {'prefix': 'pg dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    return json.loads(output.decode('utf-8'))

def get_cluster_state(cluster, logger):
    # 'pg dump' is by far the largest query, so fetch it once for both of its users
    pg_dump = get_pg_dump(cluster)

    # Gather the data we need
    cluster_state = {}
    cluster_state['osds'] = get_osd_info(cluster, logger, pg_dump)
    cluster_state['pgs'] = get_pg_info(cluster, logger, pg_dump)
    cluster_state['pools'] = get_pool_info(cluster, logger)
    cluster_state['crush_rules'] = get_crush_rules(cluster, logger)
    cluster_state['osd_bucket_maps'] = get_osd_bucket_maps(cluster, logger)
    cluster_state['backfillfull_ratio'] = get_backfillfull_ratio(cluster, logger)
    cluster_state['nearfull_ratio'] = get_nearfull_ratio(cluster, logger)
    cluster_state['upmap_queue'] = {}

    calculate_pg_distribution(cluster_state, logger)

    # Return the cluster information
    return cluster_state

def get_osd_info(cluster, logger, pg_dump=None):
    osd_info = {}

    logger.debug("Gathering info for OSDs")

    # Get the up/in status from 'osd dump'
    cmd = {'prefix': 'osd dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    osd_dump = json.loads(output.decode('utf-8'))

    for osd in osd_dump['osds']:
        osd_id = osd['osd']
        osd_info[osd_id] = {
            'up': osd['up'],
            'in': osd['in']
        }

    # Get the CRUSH weight and device class from the OSD nodes of the CRUSH
    # tree. The --show-shadow tree lists each OSD once per tree it's in, with
    # the same values each time.
    cmd = {'prefix': 'osd crush tree', 'shadow': '--show-shadow', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    crush_tree = json.loads(output.decode('utf-8'))

    # Get each OSD's size and raw usage from the per-OSD stats in 'pg dump'.
    # An OSD that hasn't reported stats (e.g. down since the mgr started) has
    # no entry; treat it as size 0.
    if pg_dump is None:
        pg_dump = get_pg_dump(cluster)
    osd_stats = {s['osd']: s for s in pg_dump['pg_map'].get('osd_stats', [])}

    # An OSD can be in the OSD map without a CRUSH location (e.g. destroyed
    # and unlinked from CRUSH but not yet removed), or in CRUSH but missing
    # from the OSD map. Give every OSD the same fields either way: no CRUSH
    # location means weight 0 and no device class.
    crush_osds = {}
    for node in crush_tree.get('nodes', []):
        if node.get('type') == 'osd':
            crush_osds[node['id']] = node
    for osd_id in set(osd_info) | set(crush_osds):
        info = osd_info.setdefault(osd_id, {'up': 0, 'in': 0})
        node = crush_osds.get(osd_id, {})
        stats = osd_stats.get(osd_id, {})
        info['crush_weight'] = node.get('crush_weight', 0.0)
        info['size'] = stats.get('kb', 0) * 1024
        if 'kb_used' in stats:
            info['used'] = stats['kb_used'] * 1024
        info['device_class'] = node.get('device_class', '')

    return osd_info

def get_pg_info(cluster, logger, pg_dump=None):
    logger.debug("Gathering info for PGs")

    # Grab the up/acting sets, state, and size for each PG
    if pg_dump is None:
        pg_dump = get_pg_dump(cluster)

    pg_info = {}
    for pg in pg_dump['pg_map']['pg_stats']:
        pg_info[pg['pgid']] = {
            'up': pg['up'].copy(),
            'acting': pg['acting'].copy(),
            'state': pg['state'].split('+'),
            'num_bytes': pg['stat_sum']['num_bytes'],
            'upmaps': []
        }

    # Grab the existing upmaps
    cmd = {'prefix': 'osd dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    osd_dump = json.loads(output.decode('utf-8'))

    # Convert the upmaps into a dictionary for easier access
    upmaps = {}
    for upmap in osd_dump['pg_upmap_items']:
        pg_info[upmap['pgid']]['upmaps'] = [(mapping['from'], mapping['to']) for mapping in upmap['mappings']]

    return pg_info

def get_pool_info(cluster, logger):
    POOL_TYPE_NAMES = {1: 'replica', 3: 'erasure'}

    logger.debug("Gathering info for pools")

    # Grab name, type (replica/erasure) pg count, crush rule, and size (replica count/erase coding shards) for each pool.
    # 'osd dump' carries the same per-pool objects as 'osd pool ls detail' (keyed 'pool' instead of 'pool_id').
    cmd = {'prefix': 'osd dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    osd_dump = json.loads(output.decode('utf-8'))

    pool_info = {}
    for pool in osd_dump['pools']:
        pool_id = pool['pool']
        pool_info[pool_id] = {
            'name': pool['pool_name'],
            'type': POOL_TYPE_NAMES[pool['type']],
            'crush_rule': pool['crush_rule'],
            'pgs': pool['pg_num'],
            'size': pool['size']
        }

        # For erasure coded pools we need to grab the k/m from each profile
        if POOL_TYPE_NAMES[pool['type']] == 'erasure':
            cmd = {'prefix': 'osd erasure-code-profile get', 'name': pool['erasure_code_profile'], 'format': 'json'}
            ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
            ec_profile = json.loads(output.decode('utf-8'))

            pool_info[pool_id]['k'] = int(ec_profile['k'])
            pool_info[pool_id]['m'] = int(ec_profile['m'])

    return pool_info

def get_osds_in_bucket(bucket_id, nodes):
    node = nodes.get(bucket_id)
    if node is None:
        return set()
    if node.get('type') == 'osd':
        return {node['id']}
    osds = set()
    for child_id in node.get('children', []):
        osds.update(get_osds_in_bucket(child_id, nodes))
    return osds

def get_crush_rules(cluster, logger):
    logger.debug("Gathering CRUSH rules")

    # Grab all the CRUSH rules
    cmd = {'prefix': 'osd crush rule dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    crush_rule_dump = json.loads(output.decode('utf-8'))

    # Grab the CRUSH tree so we can resolve which OSDs each rule can target.
    # --show-shadow makes Ceph include the device-class shadow buckets (e.g.
    # 'default~hdd'), which class-scoped rules' `take` steps reference.
    cmd = {'prefix': 'osd crush tree', 'shadow': '--show-shadow', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    crush_tree = json.loads(output.decode('utf-8'))

    nodes = {n['id']: n for n in crush_tree.get('nodes', [])}
    name_to_id = {n['name']: n['id'] for n in crush_tree.get('nodes', [])}

    crush_rules = {}
    for rule in crush_rule_dump:
        valid_osds = set()
        for step in rule['steps']:
            if step.get('op') != 'take':
                continue
            item_name = step.get('item_name')
            bucket_id = name_to_id.get(item_name)
            if bucket_id is None:
                logger.warning(f"CRUSH rule {rule['rule_name']!r} references unknown bucket {item_name!r}")
                continue
            valid_osds.update(get_osds_in_bucket(bucket_id, nodes))

        crush_rules[rule['rule_id']] = {
            'name': rule['rule_name'],
            'steps': rule['steps'],
            'valid_osds': sorted(valid_osds),
        }

    return crush_rules

def get_backfillfull_ratio(cluster, logger):
    logger.debug("Fetching backfillfull_ratio")

    # Grab the cluster's full ratio so we know the max percentage full an OSD can be
    cmd = {'prefix': 'osd dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    osd_dump = json.loads(output.decode('utf-8'))

    backfillfull_ratio = osd_dump.get('backfillfull_ratio')
    if backfillfull_ratio is None:
        logger.warning("backfillfull_ratio not found in the 'osd dump'. Defaulting to 0.95")
        backfillfull_ratio = 0.95
    logger.debug(f"Cluster backfillfull_ratio is {backfillfull_ratio}")

    return backfillfull_ratio

def get_nearfull_ratio(cluster, logger):
    logger.debug("Fetching nearfull_ratio")

    # OSDs at or above this ratio get their data moved off first
    nearfull_ratio = get_osd_dump(cluster).get('nearfull_ratio')
    if nearfull_ratio is None:
        logger.warning("nearfull_ratio not found in the 'osd dump'. Defaulting to 0.85")
        nearfull_ratio = 0.85
    logger.debug(f"Cluster nearfull_ratio is {nearfull_ratio}")

    return nearfull_ratio

def osd_utilization(pg_dump):
    """{osd: used fraction} from the per-OSD stats in 'pg dump'."""
    return {s['osd']: s.get('kb_used', 0) / s['kb']
            for s in pg_dump['pg_map'].get('osd_stats', []) if s.get('kb')}

def shard_bytes(cluster_state, pgid):
    """Bytes one OSD holds for pgid: the whole PG for replicas, num_bytes / k for an EC shard."""
    pool = cluster_state['pools'][int(pgid.split('.')[0])]
    num_bytes = cluster_state['pgs'][pgid]['num_bytes']
    return num_bytes if pool['type'] == 'replica' else num_bytes / pool['k']

def fits_below_backfillfull(cluster_state, osd_id, pgid):
    """True if one more shard of pgid keeps osd_id's projected usage within
    backfillfull_ratio. Ceph refuses backfill onto an OSD past that ratio
    (backfill_toofull), so planning such a move would just stall."""
    ratio = cluster_state.get('backfillfull_ratio')
    if ratio is None:
        return True
    size = cluster_state['osds'][osd_id].get('size', 0)
    if not size:
        return False
    return projected_usage(cluster_state, osd_id) + shard_bytes(cluster_state, pgid) <= size * ratio

def projected_usage(cluster_state, osd_id):
    """Bytes osd_id will hold once the plan's backfill completes."""
    osd = cluster_state['osds'][osd_id]
    if 'used' in osd:
        # Raw usage today (includes BlueStore overhead, omap, ...) plus the
        # net data the plan moves onto the OSD
        return osd['used'] + osd['future_usage'] - osd['current_usage']
    return osd['future_usage']

def is_past_backfillfull(cluster_state, osd_id):
    ratio = cluster_state.get('backfillfull_ratio')
    size = cluster_state['osds'][osd_id].get('size', 0)
    return ratio is not None and size > 0 and projected_usage(cluster_state, osd_id) > size * ratio

def get_rule_failure_domain(rule):
    """Return the bucket type that is the failure domain for a CRUSH rule

    Get the last choose/chooseleaf step before an emit in the CRUSH rule.
    """

    # Search for chooseleaf first
    for step in rule.get('steps', []):
        op = step.get('op', '')
        if op.startswith('chooseleaf_') or op.startswith('choose_'):
            last_step = step['type']

    return last_step

def get_osd_bucket_maps(cluster, logger):
    """Build failure-domain (rack/host/etc.) -> OSDs mappings

    For each failure-domain type referenced by a CRUSH rule, walk the CRUSH
    tree and record which OSDs are underneath it.
    """

    logger.debug("Gathering CRUSH rules")

    # Grab all the CRUSH rules
    cmd = {'prefix': 'osd crush rule dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    crush_rule_dump = json.loads(output.decode('utf-8'))

    crush_rules = {}
    for rule in crush_rule_dump:
        crush_rules[rule['rule_id']] = {
            'name': rule['rule_name'],
            'steps': rule['steps']
        }

    # Get the failure-domain for each CRUSH rule
    failure_domain_types = []
    for rule in crush_rules.values():
        failure_domain = get_rule_failure_domain(rule)
        if failure_domain not in failure_domain_types:
            failure_domain_types.append(failure_domain)

    logger.debug("Gathering CRUSH tree")

    # Grab the same --show-shadow tree get_crush_rules() uses, so a capture of
    # the cluster only needs that one tree.
    cmd = {'prefix': 'osd crush tree', 'shadow': '--show-shadow', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    crush_tree = json.loads(output.decode('utf-8'))

    logger.debug("Building failure-domain to OSDs maps")

    # Skip the device-class shadow buckets (e.g. 'host0~hdd'). Each OSD sits
    # under both its real bucket and a shadow copy of it; only the real
    # buckets name the failure domains. Ceph doesn't allow '~' in bucket
    # names, so it reliably marks a shadow bucket.
    real_nodes = [n for n in crush_tree.get('nodes', []) if '~' not in n['name']]

    # Create a mapping of all CRUSH IDs to their parent IDs
    nodes = {n['id']: n for n in real_nodes}
    parent_of = {}
    for node in real_nodes:
        for child_id in node.get('children', []):
            parent_of[child_id] = node['id']

    bucket_maps = {}
    for failure_domain_type in failure_domain_types:
        m = {}
        for node in nodes.values():
            # We only care about OSDs, so skip the others
            if node.get('type') != 'osd':
                continue
            # An OSD-level failure domain treats each OSD as its own bucket —
            # there's no enclosing 'osd' ancestor to walk up to.
            if failure_domain_type == 'osd':
                m[node['id']] = node['name']
                continue
            cur = node['id']
            while cur in parent_of:
                pid = parent_of[cur]
                parent = nodes.get(pid, {})
                if parent.get('type') == failure_domain_type:
                    m[node['id']] = parent['name']
                    break
                cur = pid
        bucket_maps[failure_domain_type] = m

    return bucket_maps

def calculate_pg_distribution(cluster_state, logger):
    logger.debug("Calculating target PG distribution per OSD per pool")

    for osd_id in cluster_state['osds']:
        cluster_state['osds'][osd_id]['target_pgs_by_pool'] = {}

    for pool_id, pool in cluster_state['pools'].items():
        valid_osds = cluster_state['crush_rules'][pool['crush_rule']]['valid_osds']

        # Treat OSDs that are marked OUT, or that have a negligible CRUSH
        # weight, as zero so they neither receive PGs nor pull share away from
        # the in-cluster OSDs that will actually host the data.
        weights = {}
        for osd_id in valid_osds:
            osd = cluster_state['osds'][osd_id]
            if not osd.get('in'):
                weights[osd_id] = 0.0
                continue
            w = osd['crush_weight']
            weights[osd_id] = w if w >= 0.0001 else 0.0
        total_weight = sum(weights.values())

        for osd_id, weight in weights.items():
            if total_weight == 0:
                ideal = 0.0
            else:
                ideal = (weight / total_weight) * pool['pgs'] * pool['size']
            cluster_state['osds'][osd_id]['target_pgs_by_pool'][pool_id] = (
                math.floor(ideal),
                math.ceil(ideal),
            )

# Loop through each pg in a pool and add the appropriate amount to each OSD
def calculate_usage(cluster_state, logger):
    logger.debug("Calculating current and future usage for each OSD")

    # Initialize current/future usage for every OSD in the cluster
    for osd in cluster_state['osds']:
        cluster_state['osds'][osd]['current_usage'] = 0
        cluster_state['osds'][osd]['current_pgs'] = 0
        cluster_state['osds'][osd]['current_pgs_by_pool'] = {}
        cluster_state['osds'][osd]['future_usage'] = 0
        cluster_state['osds'][osd]['future_pgs'] = 0
        cluster_state['osds'][osd]['future_pgs_by_pool'] = {}

    for pool in cluster_state['pools']:
        if cluster_state['pools'][pool]['type'] == 'replica':
            replica = True
        else:
            replica = False

        for pg in cluster_state['pgs']:
            if not pg.startswith(f"{pool}."):
                continue

            # Calculate current usage
            for osd in cluster_state['pgs'][pg]['acting']:
                # Skip non-existent OSDs
                if osd == CRUSH_ITEM_NONE:
                    continue

                if replica:
                    cluster_state['osds'][osd]['current_usage'] += cluster_state['pgs'][pg]['num_bytes']
                else:
                    cluster_state['osds'][osd]['current_usage'] += cluster_state['pgs'][pg]['num_bytes'] / cluster_state['pools'][pool]['k']
                cluster_state['osds'][osd]['current_pgs'] += 1
                cluster_state['osds'][osd]['current_pgs_by_pool'][pool] = \
                    cluster_state['osds'][osd]['current_pgs_by_pool'].get(pool, 0) + 1

            # Calculate future usage
            for osd in cluster_state['pgs'][pg]['up']:
                # Skip non-existent OSDs
                if osd == CRUSH_ITEM_NONE:
                    continue

                if replica:
                    cluster_state['osds'][osd]['future_usage'] += cluster_state['pgs'][pg]['num_bytes']
                else:
                    cluster_state['osds'][osd]['future_usage'] += cluster_state['pgs'][pg]['num_bytes'] / cluster_state['pools'][pool]['k']
                cluster_state['osds'][osd]['future_pgs'] += 1
                cluster_state['osds'][osd]['future_pgs_by_pool'][pool] = \
                    cluster_state['osds'][osd]['future_pgs_by_pool'].get(pool, 0) + 1

def queue_upmap_mapping_removal(cluster_state, pgid, mapping, logger):
    logger.debug(f"Queueing upmap mapping removal for {pgid}: Removing {mapping} from {cluster_state['pgs'][pgid]['upmaps']}")

    from_osd, to_osd = mapping
    pool_id = int(pgid.split('.')[0])
    pool = cluster_state['pools'][pool_id]
    num_bytes = cluster_state['pgs'][pgid]['num_bytes']
    # Each EC shard only holds num_bytes / k; replica shards hold the full PG
    per_osd_bytes = num_bytes if pool['type'] == 'replica' else num_bytes / pool['k']

    upmaps = cluster_state['pgs'][pgid]['upmaps']
    old_items = list(upmaps)
    upmaps.remove(mapping)
    update_upmap_roles(cluster_state, pgid, old_items, upmaps)

    # Reflect the restored placement in the up set so later passes (e.g.
    # balance_off_target_osds) see the PG back on from_osd, not on to_osd.
    # The redirect replaced from_osd in place, so restore it in the same slot.
    up = cluster_state['pgs'][pgid]['up']
    for i, osd in enumerate(up):
        if osd == to_osd:
            up[i] = from_osd
            break

    cluster_state['osds'][from_osd]['future_usage'] += per_osd_bytes
    cluster_state['osds'][from_osd]['future_pgs'] += 1
    cluster_state['osds'][from_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][from_osd]['future_pgs_by_pool'].get(pool_id, 0) + 1
    cluster_state['osds'][to_osd]['future_usage'] -= per_osd_bytes
    cluster_state['osds'][to_osd]['future_pgs'] -= 1
    cluster_state['osds'][to_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][to_osd]['future_pgs_by_pool'].get(pool_id, 0) - 1
    cluster_state['upmap_queue'][pgid] = cluster_state['pgs'][pgid]['upmaps']

def items_after_addition(upmaps, mapping):
    """Return a PG's upmap items after adding mapping (from_osd, to_osd).

    If from_osd is already the destination of an existing item, we'd produce
    an invalid set like [(X, from_osd), (from_osd, to_osd)] — Ceph rejects
    mappings where the same OSD appears as both source and destination.
    Collapse the chain X -> from_osd -> to_osd into a single X -> to_osd
    (or drop the item entirely when X == to_osd, since the redirect cancels).
    """
    from_osd, to_osd = mapping
    items = list(upmaps)
    chained = next((m for m in items if m[1] == from_osd), None)
    if chained is not None:
        items.remove(chained)
        if chained[0] != to_osd:
            items.append((chained[0], to_osd))
    else:
        items.append(mapping)
    return items

def count_upmap_roles(roles, pgid, items, sign):
    pool_id = int(pgid.split('.')[0])
    for from_osd, to_osd in items:
        roles[(pool_id, to_osd)][0] += sign
        roles[(pool_id, from_osd)][1] += sign

def get_upmap_roles(cluster_state):
    """(pool_id, osd_id) -> [upmap items moving data onto it, items moving data off it].

    Built from the current upmaps on first use, then kept current by
    queue_upmap_mapping_addition()/queue_upmap_mapping_removal().
    """
    roles = cluster_state.get('upmap_roles')
    if roles is None:
        roles = defaultdict(lambda: [0, 0])
        for pgid, pg in cluster_state['pgs'].items():
            count_upmap_roles(roles, pgid, pg['upmaps'], 1)
        cluster_state['upmap_roles'] = roles
    return roles

def update_upmap_roles(cluster_state, pgid, old_items, new_items):
    roles = cluster_state.get('upmap_roles')
    if roles is None:
        return
    count_upmap_roles(roles, pgid, old_items, -1)
    count_upmap_roles(roles, pgid, new_items, 1)

def would_mix_roles(cluster_state, pgid, mapping):
    """True if adding mapping would leave either OSD with upmaps moving data
    both onto and off it in the PG's pool.

    Such pairs just shuffle data through the OSD: X -> osd on one PG and
    osd -> Y on another is better expressed as X -> Y. Moving a PG off an OSD
    it was itself redirected onto is fine — the chain collapses (see
    items_after_addition()), so the OSD loses that role rather than gaining one.
    """
    roles = get_upmap_roles(cluster_state)
    pool_id = int(pgid.split('.')[0])
    old_items = cluster_state['pgs'][pgid]['upmaps']
    delta = defaultdict(lambda: [0, 0])
    count_upmap_roles(delta, pgid, old_items, -1)
    count_upmap_roles(delta, pgid, items_after_addition(old_items, mapping), 1)
    for osd_id in mapping:
        onto = roles[(pool_id, osd_id)][0] + delta[(pool_id, osd_id)][0]
        off = roles[(pool_id, osd_id)][1] + delta[(pool_id, osd_id)][1]
        if onto > 0 and off > 0:
            return True
    return False

def rebuild_upmaps(cluster_state, logger):
    """Simulate removing every upmap, then re-add only the existing ones that help.

    Undoing each PG's upmap items (last first, the reverse of how Ceph applies
    them) recovers the placement CRUSH computes without any upmaps. From there,
    an existing item is kept only if it moves a PG from an OSD over its target
    toward one under it, using the same test balance_off_target_osds() uses for
    new moves, and doesn't leave an OSD with upmaps moving data both onto and
    off it in the same pool. Items not kept are dropped, so their PGs go back
    to where CRUSH puts them; balance_off_target_osds() then fills in the rest.
    """
    osds = cluster_state['osds']
    pgs = cluster_state['pgs']

    # Undo every item. Ceph applies an item only if from_osd is in the set,
    # to_osd isn't, and to_osd isn't out; one it skipped left the set alone,
    # so there's nothing to undo — it's just dropped.
    for pgid, pg in pgs.items():
        pg['original_upmaps'] = list(pg['upmaps'])
        pg['original_up'] = list(pg['up'])
        for from_osd, to_osd in reversed(pg['original_upmaps']):
            if to_osd in pg['up'] and from_osd not in pg['up'] and osds.get(to_osd, {}).get('in', 1):
                queue_upmap_mapping_removal(cluster_state, pgid, (from_osd, to_osd), logger)
            else:
                logger.debug(f"Dropping inactive upmap {pgid}: ({from_osd} -> {to_osd})")
                old_items = list(pg['upmaps'])
                pg['upmaps'].remove((from_osd, to_osd))
                update_upmap_roles(cluster_state, pgid, old_items, pg['upmaps'])
                cluster_state['upmap_queue'][pgid] = pg['upmaps']
    cluster_state['upmap_roles'] = None
    get_upmap_roles(cluster_state)

    rules = {}
    for pool_id, pool in cluster_state['pools'].items():
        rule = cluster_state['crush_rules'][pool['crush_rule']]
        rules[pool_id] = (set(rule['valid_osds']),
                          cluster_state['osd_bucket_maps'].get(get_rule_failure_domain(rule), {}))

    def target(osd_id, pool_id):
        return osds[osd_id]['target_pgs_by_pool'].get(pool_id, (0, 0))

    def count(osd_id, pool_id):
        return osds[osd_id]['future_pgs_by_pool'].get(pool_id, 0)

    def is_helpful(pgid, from_osd, to_osd):
        pool_id = int(pgid.split('.')[0])
        valid_osds, bucket_map = rules[pool_id]
        up = pgs[pgid]['up']
        if to_osd not in valid_osds or from_osd not in up or to_osd in up:
            return False
        from_floor, from_ceil = target(from_osd, pool_id)
        to_floor, to_ceil = target(to_osd, pool_id)
        from_count, to_count = count(from_osd, pool_id), count(to_osd, pool_id)
        # Both ends stay in range, and the move fixes an imbalance
        if not (from_count > from_floor and to_count < to_ceil):
            return False
        if not (from_count > from_ceil or to_count < to_floor):
            return False
        peer_buckets = {bucket_map.get(o) for o in up if o != from_osd and o != CRUSH_ITEM_NONE}
        if bucket_map.get(to_osd) in peer_buckets:
            return False
        if not fits_below_backfillfull(cluster_state, to_osd, pgid):
            return False
        return not would_mix_roles(cluster_state, pgid, (from_osd, to_osd))

    def benefit(candidate):
        pgid, (from_osd, to_osd) = candidate
        pool_id = int(pgid.split('.')[0])
        return ((count(from_osd, pool_id) - target(from_osd, pool_id)[1]) +
                (target(to_osd, pool_id)[0] - count(to_osd, pool_id)))

    # Keep the most helpful items first. Keeping one changes the counts, so
    # make further passes until a pass keeps nothing.
    candidates = [(pgid, m) for pgid, pg in pgs.items() for m in pg['original_upmaps']]
    total = len(candidates)
    while candidates:
        candidates.sort(key=benefit, reverse=True)
        remaining = []
        for pgid, (from_osd, to_osd) in candidates:
            if is_helpful(pgid, from_osd, to_osd):
                logger.debug(f"Keeping existing upmap {pgid}: ({from_osd} -> {to_osd})")
                queue_upmap_mapping_addition(cluster_state, pgid, (from_osd, to_osd), logger)
            else:
                remaining.append((pgid, (from_osd, to_osd)))
        if len(remaining) == len(candidates):
            break
        candidates = remaining

    # Dropping an item sends its PG back to the OSD CRUSH picked. If that
    # leaves an OSD past backfillfull_ratio, restore dropped items that moved
    # data off it — largest PGs first — until it fits again, whether or not
    # they're "helpful" by PG count.
    restored = 0
    for pgid, (from_osd, to_osd) in sorted(candidates, key=lambda c: -shard_bytes(cluster_state, c[0])):
        if not is_past_backfillfull(cluster_state, from_osd):
            continue
        pool_id = int(pgid.split('.')[0])
        valid_osds, bucket_map = rules[pool_id]
        up = pgs[pgid]['up']
        if to_osd not in valid_osds or from_osd not in up or to_osd in up:
            continue
        peer_buckets = {bucket_map.get(o) for o in up if o != from_osd and o != CRUSH_ITEM_NONE}
        if bucket_map.get(to_osd) in peer_buckets:
            continue
        if not fits_below_backfillfull(cluster_state, to_osd, pgid):
            continue
        if would_mix_roles(cluster_state, pgid, (from_osd, to_osd)):
            continue
        logger.debug(f"Keeping existing upmap {pgid}: ({from_osd} -> {to_osd}) so osd.{from_osd} stays under backfillfull")
        queue_upmap_mapping_addition(cluster_state, pgid, (from_osd, to_osd), logger)
        candidates.remove((pgid, (from_osd, to_osd)))
        restored += 1

    logger.info(f"Kept {total - len(candidates)} of {total} existing upmap items"
                + (f" ({restored} to keep OSDs under backfillfull)" if restored else ""))
    for pgid, (from_osd, to_osd) in candidates:
        logger.debug(f"Dropping existing upmap {pgid}: ({from_osd} -> {to_osd})")
    for osd_id in cluster_state['osds']:
        if is_past_backfillfull(cluster_state, osd_id):
            logger.warning(f"osd.{osd_id} is still projected past backfillfull_ratio after rebuilding upmaps")

def queue_upmap_mapping_addition(cluster_state, pgid, mapping, logger):
    logger.debug(f"Queueing upmap mapping addition for {pgid}: Adding {mapping} to {cluster_state['pgs'][pgid]['upmaps']}")

    from_osd, to_osd = mapping
    pool_id = int(pgid.split('.')[0])
    pool = cluster_state['pools'][pool_id]
    num_bytes = cluster_state['pgs'][pgid]['num_bytes']
    # Each EC shard only holds num_bytes / k; replica shards hold the full PG
    per_osd_bytes = num_bytes if pool['type'] == 'replica' else num_bytes / pool['k']

    upmaps = cluster_state['pgs'][pgid]['upmaps']
    old_items = list(upmaps)
    upmaps[:] = items_after_addition(upmaps, mapping)
    update_upmap_roles(cluster_state, pgid, old_items, upmaps)

    # Reflect the new placement in the up set so subsequent iterations see it
    up = cluster_state['pgs'][pgid]['up']
    for i, osd in enumerate(up):
        if osd == from_osd:
            up[i] = to_osd
            break

    cluster_state['osds'][from_osd]['future_usage'] -= per_osd_bytes
    cluster_state['osds'][from_osd]['future_pgs'] -= 1
    cluster_state['osds'][from_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][from_osd]['future_pgs_by_pool'].get(pool_id, 0) - 1
    cluster_state['osds'][to_osd]['future_usage'] += per_osd_bytes
    cluster_state['osds'][to_osd]['future_pgs'] += 1
    cluster_state['osds'][to_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][to_osd]['future_pgs_by_pool'].get(pool_id, 0) + 1
    cluster_state['upmap_queue'][pgid] = cluster_state['pgs'][pgid]['upmaps']

def balance_off_target_osds(cluster_state, logger):
    logger.debug("Adding upmap mappings to pull off-target OSDs into their per-pool range")

    osds = cluster_state['osds']

    def count(osd_id, pool_id):
        return osds[osd_id]['future_pgs_by_pool'].get(pool_id, 0)

    def target(osd_id, pool_id):
        return osds[osd_id]['target_pgs_by_pool'].get(pool_id, (0, 0))

    for pool_id, pool in cluster_state['pools'].items():
        rule = cluster_state['crush_rules'][pool['crush_rule']]
        failure_domain = get_rule_failure_domain(rule)
        valid_osds = set(rule['valid_osds'])
        bucket_map = cluster_state['osd_bucket_maps'].get(failure_domain, {})
        pool_pgs = [(pgid, pg) for pgid, pg in cluster_state['pgs'].items()
                    if pgid.startswith(f"{pool_id}.")]
        # Set by rebuild_upmaps(): where each PG's shards are before this run
        prefer_free = any('original_up' in pg for _, pg in pool_pgs)
        is_replica = pool['type'] == 'replica'

        # osd -> indices into pool_pgs of the PGs it's in, so a donor only
        # looks at its own PGs (in pool order, which keeps results stable)
        def index_pgs():
            index = defaultdict(set)
            for i, (_, pg) in enumerate(pool_pgs):
                for osd_id in pg['up']:
                    index[osd_id].add(i)
            return index
        osd_pgs = index_pgs()

        def free_targets(pg, from_osd):
            """OSDs that already hold the data from_osd would hand over: moving
            there costs no data movement. Replicas are interchangeable; an EC
            shard is only already there if the OSD held that same shard
            position before this run."""
            original_up = pg.get('original_up', ())
            if is_replica:
                return original_up
            pos = pg['up'].index(from_osd)
            return (original_up[pos],) if pos < len(original_up) else ()

        while True:
            # Donors: OSDs whose count is strictly above their floor, so
            # donating one PG keeps them within range. Over-ceil OSDs naturally
            # sort first because their excess above floor is largest.
            donors = []
            for osd_id in valid_osds:
                floor = target(osd_id, pool_id)[0]
                if count(osd_id, pool_id) > floor:
                    donors.append((count(osd_id, pool_id) - floor, osd_id))
            if not donors:
                break
            donors.sort(reverse=True)

            # Recipients: OSDs whose count is strictly below their ceil, so
            # receiving one PG keeps them within range. Under-floor OSDs (the
            # largest room below ceil) naturally sort first.
            recipients = []
            for osd_id in valid_osds:
                ceil = target(osd_id, pool_id)[1]
                if count(osd_id, pool_id) < ceil:
                    recipients.append((ceil - count(osd_id, pool_id), osd_id))
            recipients.sort(reverse=True)
            destinations = [osd_id for _, osd_id in recipients]
            under_floor = [o for o in destinations if count(o, pool_id) < target(o, pool_id)[0]]

            made_move = False
            for _, from_osd in donors:
                donor_is_over = count(from_osd, pool_id) > target(from_osd, pool_id)[1]
                # Only commit a move that actually fixes an imbalance. If the
                # donor isn't over-ceil, the recipient must be under-floor;
                # otherwise both endpoints are already in range and moving a
                # PG between them just churns upmaps.
                dests = destinations if donor_is_over else under_floor
                if not dests:
                    continue
                dest_rank = {osd_id: i for i, osd_id in enumerate(dests)}
                # Recipients grouped by failure domain: when a PG already
                # spans most domains (e.g. 11 EC shards over 12 racks), only
                # the few recipients in its free domains need checking.
                dests_by_bucket = defaultdict(list)
                for osd_id in dests:
                    dests_by_bucket[bucket_map.get(osd_id)].append(osd_id)
                # An OSD that drain_to_at_ceil_osd() relayed a PG through may
                # pass a PG on while over its ceil, even though it now has
                # upmaps both onto and off it — that second hop is what
                # finishes the drain.
                relayed = donor_is_over and (pool_id, from_osd) in cluster_state.get('drain_relays', ())
                donor_pgs = sorted(osd_pgs[from_osd])
                # Cheap pre-checks for would_mix_roles(), which rejects most
                # candidates on clusters with many upmaps. Without a chain to
                # collapse, a move off an OSD that already receives in this
                # pool, or onto one that already gives, always mixes roles.
                roles = get_upmap_roles(cluster_state)
                donor_receives = roles.get((pool_id, from_osd), (0, 0))[0] > 0

                def gives_in_pool(osd_id):
                    return roles.get((pool_id, osd_id), (0, 0))[1] > 0

                # Moves that cost no data movement first, then any valid move
                for free_only in ((True, False) if prefer_free else (False,)):
                    for i in donor_pgs:
                        pgid, pg = pool_pgs[i]
                        if from_osd not in pg['up']:
                            continue
                        # The item (if any) that redirected this PG onto from_osd:
                        # moving on from there collapses the chain
                        chained_from = next((f for f, t in pg['upmaps'] if t == from_osd), None)
                        if not relayed and donor_receives and chained_from is None:
                            continue
                        peer_buckets = {
                            bucket_map.get(o) for o in pg['up']
                            if o != from_osd and o != CRUSH_ITEM_NONE
                        }
                        if free_only:
                            candidates = sorted((o for o in free_targets(pg, from_osd) if o in dest_rank),
                                                key=dest_rank.get)
                        else:
                            allowed = [dests_by_bucket[b] for b in dests_by_bucket if b not in peer_buckets]
                            if sum(len(group) for group in allowed) * 4 < len(dests):
                                candidates = sorted((o for group in allowed for o in group), key=dest_rank.get)
                            else:
                                candidates = dests
                        for to_osd in candidates:
                            if to_osd in pg['up']:
                                continue
                            if bucket_map.get(to_osd) in peer_buckets:
                                continue
                            if not relayed and to_osd != chained_from and gives_in_pool(to_osd):
                                continue
                            if not fits_below_backfillfull(cluster_state, to_osd, pgid):
                                continue
                            if not relayed and would_mix_roles(cluster_state, pgid, (from_osd, to_osd)):
                                continue
                            logger.info(
                                f"Adding balancing upmap {pgid}: ({from_osd} -> {to_osd}) "
                                f"[pool={pool_id}]"
                            )
                            queue_upmap_mapping_addition(cluster_state, pgid, (from_osd, to_osd), logger)
                            osd_pgs[from_osd].discard(i)
                            osd_pgs[to_osd].add(i)
                            made_move = True
                            break
                        if made_move:
                            break
                    if made_move:
                        break
                if made_move:
                    break

            if not made_move:
                made_move = drain_to_at_ceil_osd(cluster_state, pool_id, valid_osds, bucket_map, logger)
                if made_move:
                    osd_pgs = index_pgs()

            if not made_move:
                break

def drain_to_at_ceil_osd(cluster_state, pool_id, valid_osds, bucket_map, logger):
    """Move one PG off an OSD whose target is 0 when no in-range move exists.

    A ceil of 0 means the OSD is being drained (out, or CRUSH weight ~0). Its
    last PGs can get stuck when every OSD with room below its ceil is already
    in the PG or shares a failure domain with it. In that case, let the PG go
    to an OSD that's already at (or over) its ceil, least excess first. That
    OSD is then over-ceil, so the next pass of balance_off_target_osds()
    treats it as a donor and passes some other PG on to an OSD with room.

    This can't loop: it only ever moves PGs off ceil-0 OSDs, and normal moves
    never move PGs onto them (their count is never below a ceil of 0).

    It deliberately ignores would_mix_roles(): the at-ceil OSD ends up with
    upmaps moving data both onto and off it, but emptying a drained OSD wins.
    The OSD is recorded in cluster_state['drain_relays'] so its follow-up move
    is exempt too.
    """
    osds = cluster_state['osds']

    def target(osd_id):
        return osds[osd_id]['target_pgs_by_pool'].get(pool_id, (0, 0))

    def count(osd_id):
        return osds[osd_id]['future_pgs_by_pool'].get(pool_id, 0)

    draining = sorted((o for o in valid_osds if target(o)[1] == 0 and count(o) > 0),
                      key=lambda o: (-count(o), o))
    destinations = sorted((o for o in valid_osds if target(o)[1] > 0),
                          key=lambda o: (count(o) - target(o)[1], o))

    for from_osd in draining:
        for pgid, pg in cluster_state['pgs'].items():
            if not pgid.startswith(f"{pool_id}."):
                continue
            if from_osd not in pg['up']:
                continue
            peer_buckets = {
                bucket_map.get(o) for o in pg['up']
                if o != from_osd and o != CRUSH_ITEM_NONE
            }
            for to_osd in destinations:
                if to_osd in pg['up']:
                    continue
                if bucket_map.get(to_osd) in peer_buckets:
                    continue
                if not fits_below_backfillfull(cluster_state, to_osd, pgid):
                    continue
                logger.info(
                    f"Adding draining upmap {pgid}: ({from_osd} -> {to_osd}) "
                    f"[pool={pool_id}, to_count={count(to_osd)}/ceil={target(to_osd)[1]}]"
                )
                queue_upmap_mapping_addition(cluster_state, pgid, (from_osd, to_osd), logger)
                cluster_state.setdefault('drain_relays', set()).add((pool_id, to_osd))
                return True

    return False

def apply_upmap_items(raw, items, is_in):
    """The up set Ceph computes from CRUSH's raw set and a PG's pg_upmap_items.

    Mirrors OSDMap::_apply_upmap: items apply in order, and (from, to) replaces
    from only if from is in the set, to isn't, and to isn't marked out.
    """
    up = list(raw)
    for from_osd, to_osd in items:
        if to_osd in up:
            continue
        if to_osd != CRUSH_ITEM_NONE and not is_in(to_osd):
            continue
        if from_osd in up:
            up[up.index(from_osd)] = to_osd
    return up

def unapply_upmap_items(up, items, is_in):
    """CRUSH's raw set for a PG: undo its items last-first, skipping ones Ceph didn't apply."""
    raw = list(up)
    for from_osd, to_osd in reversed(items):
        if to_osd in raw and from_osd not in raw and is_in(to_osd):
            raw[raw.index(to_osd)] = from_osd
    return raw

def items_for_up(raw, up, is_in):
    """Minimal pg_upmap_items that turn CRUSH's raw set into up, or None.

    One (raw[i], up[i]) item per position that differs. An item only applies
    if its target isn't in the set yet, so an item whose target is another
    position's raw OSD has to come after the item moving that OSD out. A swap
    can't be expressed at all (Ceph doesn't allow bidirectional items).
    """
    if len(raw) != len(up) or CRUSH_ITEM_NONE in raw:
        return None
    pending = {i: (raw[i], up[i]) for i in range(len(raw)) if raw[i] != up[i]}
    current = list(raw)
    items = []
    while pending:
        ready = [i for i, (_, to_osd) in pending.items() if to_osd not in current]
        if not ready:
            return None
        i = min(ready)
        from_osd, to_osd = pending.pop(i)
        items.append((from_osd, to_osd))
        current[i] = to_osd
    if apply_upmap_items(raw, items, is_in) != list(up):
        return None
    return items

def failure_domain_conflicts(up, bucket_of):
    """How many OSDs in up share a failure domain with an earlier one."""
    buckets = [bucket_of(o) for o in up if o != CRUSH_ITEM_NONE]
    buckets = [b for b in buckets if b is not None]
    return len(buckets) - len(set(buckets))

def next_upmap_step(raw, current_up, target_up, is_in, utilization=None, bucket_of=None, spares=None,
                    spare_rank=None):
    """pg_upmap_items for the smallest step toward target_up, or None.

    Normally that moves exactly one shard, off the fullest OSD first (per
    utilization, {osd: used fraction}). Every intermediate up set must keep
    the PG's failure domains distinct (bucket_of: osd -> bucket): if it doesn't,
    the mon's upmap check (OSDMap::check_pg_upmaps -> verify_upmap) cancels
    every upmap item of the PG, not just the new one.

    No single-shard step is valid when shards have to trade failure domains:
    shard 1 moves into the domain shard 2 is leaving, shard 2 into the one
    shard 3 is leaving, ... back to shard 1. Then one shard of that cycle hops
    to a temporary OSD in a failure domain the PG neither uses nor wants
    (spares: {bucket: [osd, ...]}, best first per spare_rank), which frees its
    domain; the rest of the cycle can then move one shard at a time, and the
    hopping shard moves on to its target last. That costs one extra shard move
    but never moves several shards of a PG at once. Without a usable spare,
    change the fewest shards together that is valid.
    """
    if not (len(raw) == len(current_up) == len(target_up)):
        return None
    utilization = utilization or {}
    allowed = failure_domain_conflicts(target_up, bucket_of) if bucket_of else 0
    differing = sorted((i for i in range(len(current_up)) if current_up[i] != target_up[i]),
                       key=lambda i: (-utilization.get(current_up[i], 0), i))

    def items_if_valid(step_up):
        if len(set(step_up)) != len(step_up):
            return None     # a target OSD is still in another slot that isn't changing yet
        if bucket_of and failure_domain_conflicts(step_up, bucket_of) > allowed:
            return None
        return items_for_up(raw, step_up, is_in)

    def toward(up, positions):
        step_up = list(up)
        for i in positions:
            step_up[i] = target_up[i]
        return step_up

    for i in differing:
        items = items_if_valid(toward(current_up, (i,)))
        if items is not None:
            return items

    if spares:
        items = hop_step(current_up, target_up, differing, spares, spare_rank, bucket_of,
                         items_if_valid, toward)
        if items is not None:
            return items

    for size in range(2, len(differing) + 1):
        for combo in itertools.combinations(differing, size):
            items = items_if_valid(toward(current_up, combo))
            if items is not None:
                return items
    return None

def hop_step(current_up, target_up, differing, spares, spare_rank, bucket_of, items_if_valid, toward):
    """Items moving one shard to a temporary OSD so the rest of the PG can step
    one shard at a time, or None. See next_upmap_step()."""
    def domain(osd_id):
        return bucket_of(osd_id) if bucket_of else osd_id
    used = {domain(o) for o in list(current_up) + list(target_up) if o != CRUSH_ITEM_NONE}
    free = [o for d, group in spares.items() if d is not None and d not in used for o in group]
    if not free:
        return None
    free.sort(key=spare_rank)
    for i in differing:
        if current_up[i] == CRUSH_ITEM_NONE:
            continue
        freed = domain(current_up[i])
        # only worth it if another shard is waiting for this shard's domain
        if not any(domain(target_up[j]) == freed for j in differing if j != i):
            continue
        for spare in free:
            hop_up = list(current_up)
            hop_up[i] = spare
            items = items_if_valid(hop_up)
            if items is None:
                continue
            # the hop must let some shard reach its target on its own next
            if any(items_if_valid(toward(hop_up, (j,))) is not None for j in differing if j != i):
                return items
            break   # which domain is freed decides that, not which spare; try another shard
    return None

def is_pg_settled(pg):
    """active+clean, nothing left to backfill: safe to start the PG's next change."""
    state = set(pg['state'])
    return {'active', 'clean'} <= state and 'remapped' not in state and pg['up'] == pg['acting']

def get_osd_dump(cluster):
    cmd = {'prefix': 'osd dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    return json.loads(output.decode('utf-8'))

def plan_fingerprint(osd_dump):
    """Everything the plan depends on except upmaps, which coral itself changes."""
    return (
        osd_dump.get('crush_version'),
        tuple(sorted((o['osd'], o['up'], o['in'], o['weight']) for o in osd_dump['osds'])),
        # pg_placement_num is pgp_num: during a PG split it changes on its own
        # schedule and moves child PGs to new OSDs
        tuple(sorted((p['pool'], p['pg_num'], p.get('pg_placement_num'), p['size'], p['crush_rule'])
                     for p in osd_dump['pools'])),
    )

def align_replicas(current_up, target_up):
    """Reorder a replicated PG's target so OSDs it keeps stay in their current
    slots and arriving OSDs take the slots of leaving ones.

    Replicas are interchangeable, so only membership costs data movement.
    Stepping works slot by slot; without this, an OSD that stays in the PG but
    changes slot would have its copy moved off and then back on.
    """
    if len(current_up) != len(target_up):
        return list(target_up)
    arriving = [o for o in target_up if o not in current_up]
    return [o if o in target_up else arriving.pop(0) for o in current_up]

def build_upmap_plan(cluster_state):
    """{pgid: target up set} for every PG whose upmap items the rebuild and balancing changed."""
    plan = {}
    osds = cluster_state['osds']
    def is_in(osd_id):
        return osds.get(osd_id, {}).get('in', 1) != 0
    for pgid, pg in cluster_state['pgs'].items():
        original_items = pg.get('original_upmaps', pg['upmaps'])
        original_up = pg.get('original_up', pg['up'])
        if pg['up'] == original_up and sorted(pg['upmaps']) == sorted(original_items):
            continue
        target_up = list(pg['up'])
        if cluster_state['pools'][int(pgid.split('.')[0])]['type'] == 'replica':
            aligned = align_replicas(original_up, target_up)
            raw = unapply_upmap_items(original_up, original_items, is_in)
            if items_for_up(raw, aligned, is_in) is not None:
                target_up = aligned
        plan[pgid] = target_up
    return plan

def pool_failure_domains(cluster_state):
    """{pool_id: {osd: bucket}} for each pool's CRUSH rule failure domain."""
    domains = {}
    for pool_id, pool in cluster_state['pools'].items():
        rule = cluster_state['crush_rules'][pool['crush_rule']]
        domains[pool_id] = cluster_state['osd_bucket_maps'].get(get_rule_failure_domain(rule), {})
    return domains

def pool_rule_osds(cluster_state):
    """{pool_id: OSDs the pool's CRUSH rule can place shards on}."""
    return {pool_id: cluster_state['crush_rules'][pool['crush_rule']]['valid_osds']
            for pool_id, pool in cluster_state['pools'].items()}

def plan_upmaps(cluster, cluster_state, logger):
    """Compute target placements. Returns (plan, fingerprint of the cluster it was made for)."""
    fingerprint = plan_fingerprint(get_osd_dump(cluster))

    # Simulate removing every upmap, then keep only the existing ones that help
    rebuild_upmaps(cluster_state, logger)

    # Add new upmap mappings to pull off-target OSDs into their per-pool range
    balance_off_target_osds(cluster_state, logger)

    return build_upmap_plan(cluster_state), fingerprint

def send_upmap_items(cluster, pgid, items, is_dryrun, logger):
    """Set a PG's pg_upmap_items (or remove them all). Returns False if the mon refused."""
    if items:
        ids = [osd for pair in items for osd in pair]
        cmd = {'prefix': 'osd pg-upmap-items', 'pgid': pgid, 'id': ids, 'format': 'json'}
        text = f"ceph osd pg-upmap-items {pgid} {' '.join(str(i) for i in ids)}"
    else:
        cmd = {'prefix': 'osd rm-pg-upmap-items', 'pgid': pgid, 'format': 'json'}
        text = f"ceph osd rm-pg-upmap-items {pgid}"
    if is_dryrun:
        print(text)
        return True
    logger.info(f"Setting upmap items for {pgid}: {items}")
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    if ret != 0:
        logger.error(f"'{text}' failed ({ret}): {errs}")
        return False
    return True

def apply_upmap_round(cluster, plan, pgs, is_in, is_dryrun, logger, utilization=None, nearfull_ratio=None,
                      failure_domains=None, spare_osds=None):
    """Start the next single-shard change on every settled PG not yet at its target.

    All of a PG's backfill flows through its primary, and the PG holds a backfill
    reservation on the primary and every target OSD until the whole PG is done.
    Moving one shard per PG at a time spreads that load across many primaries
    and frees each reservation as soon as its shard lands.

    Data comes off full OSDs first. A PG is urgent if its plan moves a shard
    off an OSD at or above nearfull_ratio. While any urgent PG can step this
    round or is busy backfilling toward it, only urgent PGs start (fullest
    first); the rest wait for a later round. An urgent PG that's stuck
    (toofull, peering, degraded, ...) doesn't hold the others back. Steps
    that move no data (dropping items that no longer do anything) always go.

    utilization is {osd: used fraction}; failure_domains is {pool_id: {osd:
    bucket}} for the pool rule's failure domain, used to keep every
    intermediate step valid. spare_osds is {pool_id: [osd, ...]}, the up and
    in OSDs the pool's rule can use: when shards have to trade failure domains,
    one hops to the least loaded of these first (see next_upmap_step()).
    Returns (changes started, PGs not yet at their target).
    """
    utilization = utilization or {}
    failure_domains = failure_domains or {}
    spare_osds = spare_osds or {}
    hops = defaultdict(int)     # temporary homes picked this round, to spread them out
    spares_by_pool = {}

    def spares_for(pool_id, bucket_map):
        """{bucket: [osd, ...]} of the pool's spare OSDs that aren't nearfull."""
        if pool_id not in spares_by_pool:
            groups = defaultdict(list)
            for osd_id in spare_osds.get(pool_id, ()):
                if not is_nearfull(osd_id):
                    groups[bucket_map.get(osd_id) if bucket_map is not None else osd_id].append(osd_id)
            spares_by_pool[pool_id] = groups
        return spares_by_pool[pool_id]

    def spare_rank(osd_id):
        return hops[osd_id], utilization.get(osd_id, 0), osd_id

    def is_nearfull(osd_id):
        return nearfull_ratio is not None and utilization.get(osd_id, 0) >= nearfull_ratio

    def is_progressing(pg):
        state = set(pg['state'])
        return (bool(state & {'backfilling', 'backfill_wait', 'recovering', 'recovery_wait'})
                and not state & {'backfill_toofull', 'recovery_toofull'})

    steps = []          # (pgid, items, urgency: fullest nearfull OSD it's leaving, or 0)
    remaining = 0
    urgent_in_progress = False
    for pgid, target_up in plan.items():
        pg = pgs.get(pgid)
        if pg is None:
            logger.warning(f"PG {pgid} no longer exists; skipping")
            continue
        raw = unapply_upmap_items(pg['up'], pg['upmaps'], is_in)
        final_items = items_for_up(raw, target_up, is_in)
        if pg['up'] == target_up and final_items is not None and sorted(pg['upmaps']) == sorted(final_items):
            continue
        remaining += 1
        urgency = max((utilization.get(o, 0) for o in pg['up']
                       if o not in target_up and is_nearfull(o)), default=0)
        if not is_pg_settled(pg):
            if urgency and is_progressing(pg):
                urgent_in_progress = True
            continue
        if pg['up'] == target_up:
            # Placement is right; just drop items that no longer do anything
            items = final_items
        else:
            pool_id = int(pgid.split('.')[0])
            bucket_map = failure_domains.get(pool_id)
            items = next_upmap_step(raw, pg['up'], target_up, is_in, utilization,
                                    bucket_map.get if bucket_map is not None else None,
                                    spares_for(pool_id, bucket_map), spare_rank)
            if items is None:
                # No valid smaller step; make the whole move at once
                logger.warning(f"No valid step for {pgid} from {pg['up']} to {target_up}; moving it in one change")
                items = final_items
        if items is None:
            logger.warning(f"Can't express {pgid} target {target_up} as upmap items from CRUSH set {raw}; skipping")
            continue
        step_up = apply_upmap_items(raw, items, is_in)
        for osd_id in step_up:
            if osd_id not in target_up and osd_id not in pg['up']:
                hops[osd_id] += 1
                logger.info(f"{pgid}: moving a shard to osd.{osd_id} temporarily so its other shards "
                            f"can trade failure domains one at a time")
        moves_data = step_up != pg['up']
        steps.append((pgid, items, urgency, moves_data))

    if urgent_in_progress or any(s[2] for s in steps):
        go = [s for s in steps if s[2] or not s[3]]
        if len(go) < len(steps):
            logger.info(f"Holding back {len(steps) - len(go)} step(s) while PGs move data "
                        f"off nearfull OSDs")
        steps = go
    steps.sort(key=lambda s: s[2], reverse=True)

    started = 0
    for pgid, items, _, _ in steps:
        if send_upmap_items(cluster, pgid, items, is_dryrun, logger):
            started += 1
    return started, remaining

def run_upmap_plan(cluster, plan, fingerprint, interval, logger, sleep=time.sleep, failure_domains=None,
                   pool_osds=None):
    """Apply a plan one shard per PG per round, every interval seconds.

    Returns 'done' once every PG is at its target, or 'replan' if OSDs, weights,
    pools or the CRUSH map changed since the plan was made. pool_osds is
    {pool_id: OSDs its CRUSH rule can use}, the candidates for temporary hops.
    """
    round_no = 0
    while True:
        round_no += 1
        osd_dump = get_osd_dump(cluster)
        if plan_fingerprint(osd_dump) != fingerprint:
            logger.warning("OSDs, weights, pools or the CRUSH map changed since planning; re-planning")
            return 'replan'
        in_osds = {o['osd'] for o in osd_dump['osds'] if o['in']}
        up_in_osds = {o['osd'] for o in osd_dump['osds'] if o['in'] and o['up']}
        pg_dump = get_pg_dump(cluster)
        pgs = get_pg_info(cluster, logger, pg_dump)
        started, remaining = apply_upmap_round(
            cluster, plan, pgs, in_osds.__contains__, False, logger,
            utilization=osd_utilization(pg_dump),
            nearfull_ratio=osd_dump.get('nearfull_ratio', 0.85),
            failure_domains=failure_domains,
            spare_osds={pool_id: [o for o in osd_ids if o in up_in_osds]
                        for pool_id, osd_ids in (pool_osds or {}).items()})
        logger.info(f"Round {round_no}: started {started} single-shard change(s); "
                    f"{remaining} PG(s) not yet at their target")
        if remaining == 0:
            logger.info("Every PG is at its target placement")
            return 'done'
        sleep(interval)

def print_upmap_plan(cluster_state, plan, interval, logger):
    """Dry run: print the first round of commands and summarize the rest."""
    osds = cluster_state['osds']
    def is_in(osd_id):
        return osds.get(osd_id, {}).get('in', 0) != 0
    utilization = {o: osd['used'] / osd['size'] for o, osd in osds.items() if osd.get('size') and 'used' in osd}
    pgs = {
        pgid: {**pg, 'up': pg.get('original_up', pg['up']), 'upmaps': pg.get('original_upmaps', pg['upmaps'])}
        for pgid, pg in cluster_state['pgs'].items()
    }
    started, remaining = apply_upmap_round(None, plan, pgs, is_in, True, logger,
                                           utilization=utilization,
                                           nearfull_ratio=cluster_state.get('nearfull_ratio'),
                                           failure_domains=pool_failure_domains(cluster_state),
                                           spare_osds={pool_id: [o for o in osd_ids if osds.get(o, {}).get('up')
                                                                 and is_in(o)]
                                                       for pool_id, osd_ids in pool_rule_osds(cluster_state).items()})
    shard_moves = sum(
        sum(1 for a, b in zip(pgs[pgid]['up'], target_up) if a != b)
        for pgid, target_up in plan.items()
    )
    print(f"# {remaining} PG(s) to change ({shard_moves} shard move(s) in total); "
          f"the {started} command(s) above are the first round.")
    if remaining:
        print(f"# With --apply, coral starts each PG's next single-shard change once it's "
              f"active+clean again, checking every {interval:g}s. While any planned move "
              f"off a nearfull OSD remains, only those start.")

def display_usage(cluster_state):
    osds = cluster_state['osds']
    if not osds:
        return

    # Pre-format every value as a plain string so we can size columns from the
    # actual rendered widths rather than guessing.
    rows = []
    for osd_id in osds:
        osd = osds[osd_id]
        size_gb = osd['size'] / 1024**3
        cur_gb = osd['current_usage'] / 1024**3
        fut_gb = osd['future_usage'] / 1024**3
        cur_pgs = osd['current_pgs']
        fut_pgs = osd['future_pgs']
        cur_pct = 100 * cur_gb / size_gb if size_gb else 0.0
        fut_pct = 100 * fut_gb / size_gb if size_gb else 0.0
        rows.append({
            'osd_id':      str(osd_id),
            'class':       osd['device_class'],
            'weight':      f"{osd['crush_weight']:.5f}",
            'size':        f"{size_gb:.1f} GiB",
            'cur_gb':      f"{cur_gb:.1f} GiB",
            'cur_pct':     f"{cur_pct:.1f}%",
            'cur_pct_val': cur_pct,
            'cur_pgs':     f"{cur_pgs} PGs",
            'fut_gb':      f"{fut_gb:.1f} GiB",
            'fut_pct':     f"{fut_pct:.1f}%",
            'fut_pct_val': fut_pct,
            'fut_pgs':     f"{fut_pgs} PGs",
            'chg_gb':      f"{fut_gb - cur_gb:+.1f} GiB",
            'chg_pct':     f"{fut_pct - cur_pct:+.1f}%",
            'chg_pgs':     f"{fut_pgs - cur_pgs:+d} PGs",
        })

    def col_width(key, header):
        return max(len(header), max(len(r[key]) for r in rows))

    w_osd = col_width('osd_id', 'OSD')
    w_cls = col_width('class',  'Class')
    w_wt  = col_width('weight', 'Weight')
    w_sz  = col_width('size',   'Size')

    # Sub-widths inside each grouped column
    w_cur_gb  = max(len(r['cur_gb'])  for r in rows)
    w_cur_pct = max(len(r['cur_pct']) for r in rows)
    w_cur_pgs = max(len(r['cur_pgs']) for r in rows)
    w_fut_gb  = max(len(r['fut_gb'])  for r in rows)
    w_fut_pct = max(len(r['fut_pct']) for r in rows)
    w_fut_pgs = max(len(r['fut_pgs']) for r in rows)
    w_chg_gb  = max(len(r['chg_gb'])  for r in rows)
    w_chg_pct = max(len(r['chg_pct']) for r in rows)
    w_chg_pgs = max(len(r['chg_pgs']) for r in rows)

    SUB_SEP = '  '  # two spaces between sub-fields within a group
    w_cur = max(w_cur_gb + len(SUB_SEP) + w_cur_pct + len(SUB_SEP) + w_cur_pgs, len('Current Usage'))
    w_fut = max(w_fut_gb + len(SUB_SEP) + w_fut_pct + len(SUB_SEP) + w_fut_pgs, len('Future Usage'))
    w_chg = max(w_chg_gb + len(SUB_SEP) + w_chg_pct + len(SUB_SEP) + w_chg_pgs, len('Change'))

    col_widths = [w_osd, w_cls, w_wt, w_sz, w_cur, w_fut, w_chg]

    def border(left, mid, right):
        return left + mid.join('─' * (cw + 2) for cw in col_widths) + right

    def cell(text, width, align='right'):
        if align == 'left':
            content = text.ljust(width)
        elif align == 'center':
            content = text.center(width)
        else:
            content = text.rjust(width)
        return ' ' + content + ' '

    def pct_color(pct):
        if pct > 100: return L_RED
        if pct > 90:  return RED
        if pct > 80:  return YELLOW
        if pct > 0:   return GREEN
        return BLUE

    def grouped_section(parts, group_width):
        # parts: list of (text, width, color-or-None). Returns a string whose
        # visible width is exactly group_width, with ANSI codes injected around
        # any colored part. Right-aligned within the group cell.
        rendered = []
        visible = 0
        for i, (text, width, color) in enumerate(parts):
            if i:
                rendered.append(SUB_SEP)
                visible += len(SUB_SEP)
            padded = text.rjust(width)
            rendered.append(f"{color}{padded}{RESET}" if color else padded)
            visible += width
        pad = max(0, group_width - visible)
        return ' ' * pad + ''.join(rendered)

    print(border('┌', '┬', '┐'))
    header = '│' + '│'.join([
        cell('OSD',           w_osd),
        cell('Class',         w_cls, 'left'),
        cell('Weight',        w_wt),
        cell('Size',          w_sz),
        cell('Current Usage', w_cur, 'center'),
        cell('Future Usage',  w_fut, 'center'),
        cell('Change',        w_chg, 'center'),
    ]) + '│'
    print(header)
    print(border('├', '┼', '┤'))

    for r in rows:
        cur_section = grouped_section([
            (r['cur_gb'],  w_cur_gb,  None),
            (r['cur_pct'], w_cur_pct, pct_color(r['cur_pct_val'])),
            (r['cur_pgs'], w_cur_pgs, None),
        ], w_cur)
        fut_section = grouped_section([
            (r['fut_gb'],  w_fut_gb,  None),
            (r['fut_pct'], w_fut_pct, pct_color(r['fut_pct_val'])),
            (r['fut_pgs'], w_fut_pgs, None),
        ], w_fut)
        chg_section = grouped_section([
            (r['chg_gb'],  w_chg_gb,  None),
            (r['chg_pct'], w_chg_pct, None),
            (r['chg_pgs'], w_chg_pgs, None),
        ], w_chg)

        line = '│' + '│'.join([
            cell(r['osd_id'], w_osd),
            cell(r['class'],  w_cls, 'left'),
            cell(r['weight'], w_wt),
            cell(r['size'],   w_sz),
            ' ' + cur_section + ' ',
            ' ' + fut_section + ' ',
            ' ' + chg_section + ' ',
        ]) + '│'
        print(line)

    print(border('└', '┴', '┘'))

def display_pgs_by_pool(cluster_state):
    # For each OSD, print a small table showing the future PG count, target
    # floor/ceil, and status (OVER/UNDER/OK) per pool. Pools where the OSD
    # has neither a target entry nor any future PGs are skipped.
    def _pool_name(pool_id):
        return cluster_state['pools'].get(pool_id, {}).get('name', f"pool_{pool_id}")

    # Build per-OSD row sets first so we can size every table uniformly.
    sections = []
    for osd_id in cluster_state['osds']:
        osd = cluster_state['osds'][osd_id]
        target_by_pool = osd.get('target_pgs_by_pool', {})
        future_by_pool = osd.get('future_pgs_by_pool', {})
        pool_ids = sorted(set(target_by_pool) | set(future_by_pool))
        if not pool_ids:
            continue
        rows = []
        for pool_id in pool_ids:
            future_pgs = future_by_pool.get(pool_id, 0)
            floor, ceil = target_by_pool.get(pool_id, (0, 0))
            if future_pgs > ceil:
                status, status_color = 'OVER', RED
            elif future_pgs < floor:
                status, status_color = 'UNDER', YELLOW
            else:
                status, status_color = 'OK', GREEN
            rows.append({
                'pool':       _pool_name(pool_id),
                'future_pgs': str(future_pgs),
                'floor':      str(floor),
                'ceil':       str(ceil),
                'status':     status,
                'color':      status_color,
            })
        sections.append((osd_id, rows))

    if not sections:
        return

    # Pool column sizes to the longest pool name across every OSD; the numeric
    # columns size to whichever value is widest. Keeps tables aligned across
    # OSDs even when one has long names like default.rgw.buckets.index.
    all_rows = [r for _, rows in sections for r in rows]
    w_pool   = max([len('Pool')]       + [len(r['pool'])       for r in all_rows])
    w_future = max([len('Future PGs')] + [len(r['future_pgs']) for r in all_rows])
    w_floor  = max([len('Floor')]      + [len(r['floor'])      for r in all_rows])
    w_ceil   = max([len('Ceil')]       + [len(r['ceil'])       for r in all_rows])
    w_status = max([len('Status')]     + [len(r['status'])     for r in all_rows])

    col_widths = [w_pool, w_future, w_floor, w_ceil, w_status]

    def border(left, mid, right):
        return left + mid.join('─' * (cw + 2) for cw in col_widths) + right

    def cell(text, width, align='right'):
        if align == 'left':
            content = text.ljust(width)
        elif align == 'center':
            content = text.center(width)
        else:
            content = text.rjust(width)
        return ' ' + content + ' '

    def colored_cell(text, width, color, align='center'):
        # Pad first so visible width matches the column, then wrap with ANSI.
        if align == 'left':
            padded = text.ljust(width)
        elif align == 'right':
            padded = text.rjust(width)
        else:
            padded = text.center(width)
        return ' ' + color + padded + RESET + ' '

    top    = border('┌', '┬', '┐')
    middle = border('├', '┼', '┤')
    bottom = border('└', '┴', '┘')
    header = '│' + '│'.join([
        cell('Pool',       w_pool, 'left'),
        cell('Future PGs', w_future),
        cell('Floor',      w_floor),
        cell('Ceil',       w_ceil),
        cell('Status',     w_status, 'center'),
    ]) + '│'

    for i, (osd_id, rows) in enumerate(sections):
        if i:
            print()
        print(f"{CYAN}osd.{osd_id}{RESET}")
        print(top)
        print(header)
        print(middle)
        for r in rows:
            line = '│' + '│'.join([
                cell(r['pool'],       w_pool, 'left'),
                cell(r['future_pgs'], w_future),
                cell(r['floor'],      w_floor),
                cell(r['ceil'],       w_ceil),
                colored_cell(r['status'], w_status, r['color'], 'center'),
            ]) + '│'
            print(line)
        print(bottom)

if __name__ == '__main__':
    main()
