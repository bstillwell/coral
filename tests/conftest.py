import sys
import json
from unittest.mock import MagicMock
import pytest

# Must happen before coral is imported anywhere
sys.modules["rados"] = MagicMock()

import coral  # noqa: E402

GiB = 1024 ** 3

OSD_DUMP = {
    "osds": [
        {"osd": 0, "up": 1, "in": 1},
        {"osd": 1, "up": 1, "in": 1},
    ],
    "backfillfull_ratio": 0.9,
    "pg_upmap_items": [
        {"pgid": "1.0", "mappings": [{"from": 0, "to": 1}]}
    ],
}

OSD_DF = {
    "nodes": [
        {"id": 0, "crush_weight": 1.0, "kb": 10 * 1024 * 1024, "device_class": "hdd"},
        {"id": 1, "crush_weight": 1.0, "kb": 10 * 1024 * 1024, "device_class": "hdd"},
    ]
}

PG_DUMP = {
    "pg_map": {
        "pg_stats": [
            {
                "pgid": "1.0",
                "up": [1],
                "acting": [0],
                "state": "active+backfilling",
                "stat_sum": {"num_bytes": GiB},
            }
        ]
    }
}

POOL_LS_REPLICA = [
    {"pool_id": 1, "pool_name": "rbd", "type": 1, "crush_rule": 0, "pg_num": 128, "size": 3}
]

POOL_LS_EC = [
    {
        "pool_id": 1,
        "pool_name": "ecpool",
        "type": 3,
        "crush_rule": 0,
        "pg_num": 64,
        "size": 6,
        "erasure_code_profile": "ec-4-2",
    }
]

EC_PROFILE = {"k": "4", "m": "2", "plugin": "jerasure", "technique": "reed_sol_van"}

CRUSH_RULE_DUMP = [
    {
        "rule_id": 0,
        "rule_name": "replicated_rule",
        "steps": [
            {"op": "take", "item_name": "default"},
            {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
            {"op": "emit"},
        ],
    }
]

CRUSH_TREE = {
    "nodes": [
        {"id": -1, "name": "default", "type": "root", "children": [-2, -3]},
        {"id": -2, "name": "host0", "type": "host", "children": [0]},
        {"id": -3, "name": "host1", "type": "host", "children": [1]},
        {"id": 0, "name": "osd.0", "type": "osd", "children": []},
        {"id": 1, "name": "osd.1", "type": "osd", "children": []},
    ]
}


def make_mock_cluster(overrides=None):
    responses = {
        "osd dump": OSD_DUMP,
        "osd df": OSD_DF,
        "pg dump": PG_DUMP,
        "osd pool ls": POOL_LS_REPLICA,
        "osd erasure-code-profile get": EC_PROFILE,
        "osd crush rule dump": CRUSH_RULE_DUMP,
        "osd crush tree": CRUSH_TREE,
    }
    if overrides:
        responses.update(overrides)

    def mon_command(cmd_json, inbuf, timeout=5):
        cmd = json.loads(cmd_json)
        data = responses[cmd["prefix"]]
        return 0, json.dumps(data).encode("utf-8"), b""

    cluster = MagicMock()
    cluster.mon_command.side_effect = mon_command
    return cluster


@pytest.fixture
def mock_cluster():
    return make_mock_cluster()


@pytest.fixture
def logger():
    return MagicMock()


@pytest.fixture
def balance_cluster_state():
    # 3 OSDs on 3 distinct hosts; pool 1 (replica size 2). OSD 0 is over-target,
    # OSD 2 is under-target — a single upmap (0 -> 2) on PG 1.0 should balance.
    return {
        "osds": {
            0: {
                "size": 10 * GiB,
                "current_usage": GiB, "current_pgs": 1, "current_pgs_by_pool": {1: 1},
                "future_usage": GiB, "future_pgs": 1, "future_pgs_by_pool": {1: 1},
                "target_pgs_by_pool": {1: (0, 0)},
            },
            1: {
                "size": 10 * GiB,
                "current_usage": GiB, "current_pgs": 1, "current_pgs_by_pool": {1: 1},
                "future_usage": GiB, "future_pgs": 1, "future_pgs_by_pool": {1: 1},
                "target_pgs_by_pool": {1: (1, 1)},
            },
            2: {
                "size": 10 * GiB,
                "current_usage": 0, "current_pgs": 0, "current_pgs_by_pool": {1: 0},
                "future_usage": 0, "future_pgs": 0, "future_pgs_by_pool": {1: 0},
                "target_pgs_by_pool": {1: (1, 1)},
            },
        },
        "pgs": {
            "1.0": {
                "up": [0, 1],
                "acting": [0, 1],
                "state": ["active", "clean"],
                "num_bytes": GiB,
                "upmaps": [],
            }
        },
        "pools": {
            1: {"name": "rbd", "type": "replica", "crush_rule": 0, "pgs": 1, "size": 2}
        },
        "crush_rules": {
            0: {
                "name": "replicated_rule",
                "steps": [
                    {"op": "take", "item_name": "default"},
                    {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
                    {"op": "emit"},
                ],
                "valid_osds": [0, 1, 2],
            }
        },
        "osd_bucket_maps": {
            "host": {0: "host0", 1: "host1", 2: "host2"},
        },
        "backfillfull_ratio": 0.9,
        "upmap_queue": {},
    }


@pytest.fixture
def base_cluster_state():
    return {
        "osds": {
            0: {
                "size": 10 * GiB,
                "current_usage": 0,
                "current_pgs": 1,
                "current_pgs_by_pool": {1: 1},
                "future_usage": 0,
                "future_pgs": 0,
                "future_pgs_by_pool": {1: 0},
                "target_pgs_by_pool": {1: (0, 1)},
            },
            1: {
                "size": 10 * GiB,
                "current_usage": 0,
                "current_pgs": 0,
                "current_pgs_by_pool": {1: 0},
                "future_usage": GiB,
                "future_pgs": 1,
                "future_pgs_by_pool": {1: 1},
                "target_pgs_by_pool": {1: (0, 1)},
            },
        },
        "pgs": {
            "1.0": {
                "up": [1],
                "acting": [0],
                "state": ["active", "backfilling"],
                "num_bytes": GiB,
                "upmaps": [(0, 1)],
            }
        },
        "pools": {
            1: {"name": "rbd", "type": "replica", "crush_rule": 0, "pgs": 1, "size": 3}
        },
        "backfillfull_ratio": 0.9,
        "upmap_queue": {},
    }
