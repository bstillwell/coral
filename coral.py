#!/usr/bin/env python3

import json
import rados

BLUE = '\033[0;34m'
CYAN = '\033[0;36m'
GREEN = '\033[0;32m'
RED = '\033[0;31m'
L_RED = '\033[1;31m'
YELLOW = '\033[0;33m'
RESET = '\033[0m'

def main():
    cluster_info = get_cluster_info()

    calculate_usage(cluster_info)
    display_usage(cluster_info)

def get_cluster_info():
    # Connect to the cluster
    cluster = rados.Rados(conffile="/etc/ceph/ceph.conf")
    cluster.connect()

    # Gather the data we need
    cluster_info = {}
    cluster_info['osds'] = get_osd_info(cluster)
    cluster_info['pgs'] = get_pg_info(cluster)
    cluster_info['pools'] = get_pool_info(cluster)
    cluster_info['crush_rules'] = get_crush_rules(cluster)

    # Disconnect from the cluster
    cluster.shutdown()

    # Return the cluster information
    return cluster_info

def get_osd_info(cluster):
    osd_info = {}

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

def get_pg_info(cluster):
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

def get_pool_info(cluster):
    POOL_TYPE_NAMES = {1: 'replica', 3: 'erasure'}

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

    return pool_info

def get_crush_rules(cluster):
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
def calculate_usage(cluster_info):
    # Initialize current/future usage for every OSD in the cluster
    for osd in cluster_info['osds']:
        cluster_info['osds'][osd]['current_usage'] = 0
        cluster_info['osds'][osd]['current_pgs'] = 0
        cluster_info['osds'][osd]['future_usage'] = 0
        cluster_info['osds'][osd]['future_pgs'] = 0

    for pool in cluster_info['pools']:
        for pg in cluster_info['pgs']:
            if not pg.startswith(f"{pool}."):
                continue

            # Calculate current usage
            for osd in cluster_info['pgs'][pg]['acting']:
                cluster_info['osds'][osd]['current_usage'] += cluster_info['pgs'][pg]['num_bytes']
                cluster_info['osds'][osd]['current_pgs'] += 1

            # Calculate future usage
            for osd in cluster_info['pgs'][pg]['up']:
                cluster_info['osds'][osd]['future_usage'] += cluster_info['pgs'][pg]['num_bytes']
                cluster_info['osds'][osd]['future_pgs'] += 1

def display_usage(cluster_info):
    # Calculate data per OSD after backfilling is complete
    print("OSD  | Class | Weight   | Size        | Current Usage              | Future Usage               | Change")
    print("-----+-------+----------+-------------+----------------------------+----------------------------+----------------------------")

    for osd in cluster_info['osds']:
        device_class = cluster_info['osds'][osd]['device_class']
        crush_weight = cluster_info['osds'][osd]['crush_weight']
        drive_size = cluster_info['osds'][osd]['size'] / 1024**3

        current_usage_gb = cluster_info['osds'][osd]['current_usage'] / 1024**3
        current_pgs = cluster_info['osds'][osd]['current_pgs']

        future_usage_gb = cluster_info['osds'][osd]['future_usage'] / 1024**3
        future_pgs = cluster_info['osds'][osd]['future_pgs']

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
