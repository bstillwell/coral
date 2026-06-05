#!/usr/bin/env python3

import argparse
import json
import logging
import math
import rados
import sys

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
                        help="Show the future cluster state")
    parser.add_argument('--dry-run', action='store_true',
                        help="Don't modify the pg upmaps, print out the pg-upmap-items commands instead")
    args = parser.parse_args()

    is_preview = args.preview
    is_dryrun = args.dry_run or is_preview

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

    # TODO: re-enable once remove_off_target_mappings is rewritten to reduce
    # the total upmap mapping count
    # remove_off_target_mappings(cluster_state, logger)

    # Add new upmap mappings to pull off-target OSDs into their per-pool range
    balance_off_target_osds(cluster_state, logger)

    # Apply the changes we determined were needed
    apply_upmap_queue(cluster, cluster_state, is_dryrun, logger)

    # Provide a nice display of current and future OSD usage
    display_usage(cluster_state)

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

def get_cluster_state(cluster, logger):
    # Gather the data we need
    cluster_state = {}
    cluster_state['osds'] = get_osd_info(cluster, logger)
    cluster_state['pgs'] = get_pg_info(cluster, logger)
    cluster_state['pools'] = get_pool_info(cluster, logger)
    cluster_state['crush_rules'] = get_crush_rules(cluster, logger)
    cluster_state['osd_bucket_maps'] = get_osd_bucket_maps(cluster, logger)
    cluster_state['backfillfull_ratio'] = get_backfillfull_ratio(cluster, logger)
    cluster_state['upmap_queue'] = {}

    calculate_pg_distribution(cluster_state, logger)

    # Return the cluster information
    return cluster_state

def get_osd_info(cluster, logger):
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

    # Get the CRUSH weight, size, and device class from 'osd df'
    cmd = {'prefix': 'osd df', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    osd_df = json.loads(output.decode('utf-8'))

    for osd in osd_df['nodes']:
        osd_info[osd['id']]['crush_weight'] = osd['crush_weight']
        osd_info[osd['id']]['size'] = osd['kb'] * 1024
        osd_info[osd['id']]['device_class'] = osd['device_class']

    return osd_info

def get_pg_info(cluster, logger):
    logger.debug("Gathering info for PGs")

    # Grab the up/acting sets, state, and size for each PG
    cmd = {'prefix': 'pg dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    pg_dump = json.loads(output.decode('utf-8'))

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

    # Grab name, type (replica/erasure) pg count, crush rule, and size (replica count/erase coding shards) for each pool
    cmd = {'prefix': 'osd pool ls', 'detail': 'detail', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    pool_info_full = json.loads(output.decode('utf-8'))

    pool_info = {}
    for pool in pool_info_full:
        pool_info[pool['pool_id']] = {
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

            pool_info[pool['pool_id']]['k'] = int(ec_profile['k'])
            pool_info[pool['pool_id']]['m'] = int(ec_profile['m'])

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

    # Grab the CRUSH tree
    cmd = {'prefix': 'osd crush tree', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    crush_tree = json.loads(output.decode('utf-8'))

    logger.debug("Building failure-domain to OSDs maps")

    # Create a mapping of all CRUSH IDs to their parent IDs
    nodes = {n['id']: n for n in crush_tree.get('nodes', [])}
    parent_of = {}
    for node in crush_tree.get('nodes', []):
        for child_id in node.get('children', []):
            parent_of[child_id] = node['id']

    bucket_maps = {}
    for failure_domain_type in failure_domain_types:
        m = {}
        for node in crush_tree.get('nodes', []):
            # We only care about OSDs, so skip the others
            if node.get('type') != 'osd':
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

        # Treat negligible-weight OSDs as zero so they neither receive PGs nor
        # affect the proportions for the rest
        weights = {}
        for osd_id in valid_osds:
            w = cluster_state['osds'][osd_id]['crush_weight']
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
    cluster_state['pgs'][pgid]['upmaps'].remove(mapping)
    cluster_state['osds'][from_osd]['future_usage'] += cluster_state['pgs'][pgid]['num_bytes']
    cluster_state['osds'][from_osd]['future_pgs'] += 1
    cluster_state['osds'][from_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][from_osd]['future_pgs_by_pool'].get(pool_id, 0) + 1
    cluster_state['osds'][to_osd]['future_usage'] -= cluster_state['pgs'][pgid]['num_bytes']
    cluster_state['osds'][to_osd]['future_pgs'] -= 1
    cluster_state['osds'][to_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][to_osd]['future_pgs_by_pool'].get(pool_id, 0) - 1
    cluster_state['upmap_queue'][pgid] = cluster_state['pgs'][pgid]['upmaps']

def remove_off_target_mappings(cluster_state, logger):
    logger.debug("Scanning for upmaps causing OSDs to be off-target for their pool")

    osds = cluster_state['osds']

    for pgid, pg_info in cluster_state['pgs'].items():
        pool_id = int(pgid.split('.')[0])
        for from_osd, to_osd in list(pg_info['upmaps']):
            to_ceil = osds[to_osd]['target_pgs_by_pool'].get(pool_id, (0, 0))[1]
            from_floor = osds[from_osd]['target_pgs_by_pool'].get(pool_id, (0, 0))[0]
            to_count = osds[to_osd]['future_pgs_by_pool'].get(pool_id, 0)
            from_count = osds[from_osd]['future_pgs_by_pool'].get(pool_id, 0)

            if to_count > to_ceil or from_count < from_floor:
                logger.info(
                    f"Removing off-target upmap {pgid}: ({from_osd} -> {to_osd}) "
                    f"[to_count={to_count}/ceil={to_ceil}, "
                    f"from_count={from_count}/floor={from_floor}]"
                )
                queue_upmap_mapping_removal(cluster_state, pgid, (from_osd, to_osd), logger)

def queue_upmap_mapping_addition(cluster_state, pgid, mapping, logger):
    logger.debug(f"Queueing upmap mapping addition for {pgid}: Adding {mapping} to {cluster_state['pgs'][pgid]['upmaps']}")

    from_osd, to_osd = mapping
    pool_id = int(pgid.split('.')[0])
    cluster_state['pgs'][pgid]['upmaps'].append(mapping)

    # Reflect the new placement in the up set so subsequent iterations see it
    up = cluster_state['pgs'][pgid]['up']
    for i, osd in enumerate(up):
        if osd == from_osd:
            up[i] = to_osd
            break

    cluster_state['osds'][from_osd]['future_usage'] -= cluster_state['pgs'][pgid]['num_bytes']
    cluster_state['osds'][from_osd]['future_pgs'] -= 1
    cluster_state['osds'][from_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][from_osd]['future_pgs_by_pool'].get(pool_id, 0) - 1
    cluster_state['osds'][to_osd]['future_usage'] += cluster_state['pgs'][pgid]['num_bytes']
    cluster_state['osds'][to_osd]['future_pgs'] += 1
    cluster_state['osds'][to_osd]['future_pgs_by_pool'][pool_id] = \
        cluster_state['osds'][to_osd]['future_pgs_by_pool'].get(pool_id, 0) + 1
    cluster_state['upmap_queue'][pgid] = cluster_state['pgs'][pgid]['upmaps']

def balance_off_target_osds(cluster_state, logger):
    logger.debug("Adding upmap mappings to pull off-target OSDs into their per-pool range")

    osds = cluster_state['osds']

    for pool_id, pool in cluster_state['pools'].items():
        rule = cluster_state['crush_rules'][pool['crush_rule']]
        failure_domain = get_rule_failure_domain(rule)
        valid_osds = set(rule['valid_osds'])
        bucket_map = cluster_state['osd_bucket_maps'].get(failure_domain, {})

        while True:
            over = []
            for osd_id in valid_osds:
                ceil = osds[osd_id]['target_pgs_by_pool'].get(pool_id, (0, 0))[1]
                count = osds[osd_id]['future_pgs_by_pool'].get(pool_id, 0)
                if count > ceil:
                    over.append((count - ceil, osd_id))
            if not over:
                break
            over.sort(reverse=True)
            _, from_osd = over[0]

            under = []
            for osd_id in valid_osds:
                floor = osds[osd_id]['target_pgs_by_pool'].get(pool_id, (0, 0))[0]
                count = osds[osd_id]['future_pgs_by_pool'].get(pool_id, 0)
                if count < floor:
                    under.append((floor - count, osd_id))
            if not under:
                break
            under.sort(reverse=True)
            destinations = [osd_id for _, osd_id in under]

            made_move = False
            for pgid, pg in cluster_state['pgs'].items():
                if not pgid.startswith(f"{pool_id}."):
                    continue
                if from_osd not in pg['up']:
                    continue
                for to_osd in destinations:
                    if to_osd in pg['up']:
                        continue
                    to_bucket = bucket_map.get(to_osd)
                    peer_buckets = {
                        bucket_map.get(o) for o in pg['up']
                        if o != from_osd and o != CRUSH_ITEM_NONE
                    }
                    if to_bucket in peer_buckets:
                        continue
                    logger.info(
                        f"Adding balancing upmap {pgid}: ({from_osd} -> {to_osd}) "
                        f"[pool={pool_id}]"
                    )
                    queue_upmap_mapping_addition(cluster_state, pgid, (from_osd, to_osd), logger)
                    made_move = True
                    break
                if made_move:
                    break

            if not made_move:
                break

def apply_upmap_queue(cluster, cluster_state, is_dryrun, logger):
    logger.debug("Applying upmap changes")

    upmap_queue = cluster_state['upmap_queue']
    for pgid in upmap_queue:
        mappings = upmap_queue[pgid]
        if len(mappings) == 0:
            if is_dryrun:
                print(f"ceph osd rm-pg-upmap-items {pgid}")
            else:
                logger.info(f"Removing upmap items for {pgid}")
                cmd = {'prefix': 'osd rm-pg-upmap-items', 'pgid': pgid, 'format': 'json'}
                ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
        else:
            mappings_list = [str(item) for osd in mappings for item in osd]
            if is_dryrun:
                print(f"ceph osd pg-upmap-items {pgid} {' '.join(mappings_list)}")
            else:
                logger.info(f"Applying upmap items for {pgid}: {mappings}")
                cmd = {'prefix': 'osd pg-upmap-items', 'pgid': pgid, 'id': mappings_list, 'format': 'json'}
                ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)

def display_usage(cluster_state):
    # Calculate data per OSD after backfilling is complete
    print("OSD  | Class | Weight   | Size        | Current Usage              | Future Usage               | Change")
    print("-----+-------+----------+-------------+----------------------------+----------------------------+----------------------------")

    for osd in cluster_state['osds']:
        device_class = cluster_state['osds'][osd]['device_class']
        crush_weight = cluster_state['osds'][osd]['crush_weight']
        drive_size = cluster_state['osds'][osd]['size'] / 1024**3

        current_usage_gb = cluster_state['osds'][osd]['current_usage'] / 1024**3
        current_pgs = cluster_state['osds'][osd]['current_pgs']

        future_usage_gb = cluster_state['osds'][osd]['future_usage'] / 1024**3
        future_pgs = cluster_state['osds'][osd]['future_pgs']

        if drive_size != 0:
            current_usage_pct = 100*(current_usage_gb / drive_size)
            future_usage_pct = 100*(future_usage_gb / drive_size)
        else:
            current_usage_pct = 0
            future_usage_pct = 0

        change_usage_gb = future_usage_gb - current_usage_gb
        change_usage_pct = future_usage_pct - current_usage_pct
        change_pgs = future_pgs - current_pgs

        osd_stat_strings = []
        osd_stat_strings.append(f"{osd:<4}")
        osd_stat_strings.append(f"{device_class:<5}")
        osd_stat_strings.append(f"{crush_weight:>8.5f}")
        osd_stat_strings.append(f"{drive_size:>7.1f} GiB")

        if current_usage_pct > 100:
            PCT_COLOR = L_RED
        elif current_usage_pct > 90:
            PCT_COLOR = RED
        elif current_usage_pct > 80:
            PCT_COLOR = YELLOW
        elif current_usage_pct > 0:
            PCT_COLOR = GREEN
        else:
            PCT_COLOR = BLUE
        osd_stat_strings.append(f"{current_usage_gb:>5.1f} GiB  {PCT_COLOR}{current_usage_pct:5.1f}%{RESET}  {current_pgs:>3} PGs")

        if future_usage_pct > 100:
            PCT_COLOR = L_RED
        elif future_usage_pct > 90:
            PCT_COLOR = RED
        elif future_usage_pct > 80:
            PCT_COLOR = YELLOW
        elif future_usage_pct > 0:
            PCT_COLOR = GREEN
        else:
            PCT_COLOR = BLUE
        osd_stat_strings.append(f"{future_usage_gb:>5.1f} GiB  {PCT_COLOR}{future_usage_pct:5.1f}%{RESET}  {future_pgs:>3} PGs")
        osd_stat_strings.append(f"{change_usage_gb:+5.1f} GiB  {change_usage_pct:+6.1f}%  {change_pgs:+3} PGs")

        print(" | ".join(osd_stat_strings))

if __name__ == '__main__':
    main()
