#!/usr/bin/env python3

import argparse
import json
import logging
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
    parser.add_argument('--preview', action='store_true', help="Show the future cluster state")
    args = parser.parse_args()

    logger = setup_logging("/var/log/ceph/coral.log", is_preview=args.preview)
    logger.info("Initializing Coral Balancer")

    # Connect to the cluster
    logger.debug("Connecting to Ceph cluster")
    cluster = rados.Rados(conffile="/etc/ceph/ceph.conf")
    cluster.connect()

    # Get the current state of the cluster
    cluster_state = get_cluster_state(cluster, logger)

    # Disconnect from the cluster
    logger.debug("Disconnecting from Ceph cluster")
    cluster.shutdown()

    calculate_usage(cluster_state, logger)
    display_usage(cluster_state)

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
    cluster_state['full_ratio'] = get_full_ratio(cluster, logger)

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
            'state': pg['state'],
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

def get_full_ratio(cluster, logger):
    logger.debug("Fetching full_ratio")

    # Grab the cluster's full ratio so we know the max percentage full an OSD can be
    cmd = {'prefix': 'osd dump', 'format': 'json'}
    ret, output, errs = cluster.mon_command(json.dumps(cmd), b'', timeout=5)
    osd_dump = json.loads(output.decode('utf-8'))

    full_ratio = osd_dump.get('full_ratio')
    if full_ratio is None:
        logger.warning("full_ratio not found in the 'osd dump'. Defaulting to 0.95")
        full_ratio = 0.95
    logger.debug(f"Cluster full_ratio is {full_ratio}")

    return full_ratio

def get_crush_rules(cluster, logger):
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

    return crush_rules

# Loop through each pg in a pool and add the appropriate amount to each OSD
def calculate_usage(cluster_state, logger):
    logger.debug("Calculating current and future usage for each OSD")

    # Initialize current/future usage for every OSD in the cluster
    for osd in cluster_state['osds']:
        cluster_state['osds'][osd]['current_usage'] = 0
        cluster_state['osds'][osd]['current_pgs'] = 0
        cluster_state['osds'][osd]['future_usage'] = 0
        cluster_state['osds'][osd]['future_pgs'] = 0

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
