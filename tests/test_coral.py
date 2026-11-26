import json
import re
from unittest.mock import MagicMock
import pytest

import coral
from conftest import make_mock_cluster, GiB, OSD_DUMP, POOLS_EC, PG_DUMP, CRUSH_TREE


class TestGetRuleFailureDomain:
    def test_chooseleaf_step_returns_its_type(self):
        rule = {
            "steps": [
                {"op": "take", "item_name": "default"},
                {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
                {"op": "emit"},
            ]
        }
        assert coral.get_rule_failure_domain(rule) == "host"

    def test_choose_step_returns_its_type(self):
        rule = {
            "steps": [
                {"op": "take", "item_name": "default"},
                {"op": "choose_firstn", "num": 1, "type": "rack"},
                {"op": "emit"},
            ]
        }
        assert coral.get_rule_failure_domain(rule) == "rack"

    def test_last_matching_step_wins(self):
        rule = {
            "steps": [
                {"op": "choose_firstn", "num": 1, "type": "rack"},
                {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
                {"op": "emit"},
            ]
        }
        assert coral.get_rule_failure_domain(rule) == "host"


class TestGetOsdInfo:
    def test_up_in_status(self, mock_cluster, logger):
        result = coral.get_osd_info(mock_cluster, logger)
        assert result[0]["up"] == 1
        assert result[0]["in"] == 1

    def test_size_converted_to_bytes(self, mock_cluster, logger):
        result = coral.get_osd_info(mock_cluster, logger)
        assert result[0]["size"] == 10 * GiB

    def test_device_class(self, mock_cluster, logger):
        result = coral.get_osd_info(mock_cluster, logger)
        assert result[0]["device_class"] == "hdd"

    def test_crush_weight(self, mock_cluster, logger):
        result = coral.get_osd_info(mock_cluster, logger)
        assert result[0]["crush_weight"] == 1.0

    def test_osd_without_stats_has_zero_size(self, logger):
        pg_dump = {"pg_map": {**PG_DUMP["pg_map"], "osd_stats": [{"osd": 0, "kb": 1024}]}}
        cluster = make_mock_cluster({"pg dump": pg_dump})
        result = coral.get_osd_info(cluster, logger)
        assert result[0]["size"] == 1024 * 1024
        assert result[1]["size"] == 0

    def test_osd_listed_in_several_shadow_trees(self, logger):
        tree = {"nodes": CRUSH_TREE["nodes"] + [
            {"id": -10, "name": "default~hdd", "type": "root", "children": [-11]},
            {"id": -11, "name": "host0~hdd", "type": "host", "children": [0]},
            {"id": 0, "name": "osd.0", "type": "osd", "children": [], "crush_weight": 1.0, "device_class": "hdd"},
        ]}
        cluster = make_mock_cluster({"osd crush tree": tree})
        result = coral.get_osd_info(cluster, logger)
        assert result[0]["crush_weight"] == 1.0
        assert result[0]["device_class"] == "hdd"

    def test_osd_without_crush_location(self, logger):
        # osd.2 is in the OSD map (down/out) but was unlinked from CRUSH
        osd_dump = {**OSD_DUMP, "osds": OSD_DUMP["osds"] + [{"osd": 2, "up": 0, "in": 0}]}
        result = coral.get_osd_info(make_mock_cluster({"osd dump": osd_dump}), logger)
        assert result[2] == {"up": 0, "in": 0, "crush_weight": 0.0, "size": 0, "device_class": ""}

    def test_osd_in_crush_but_not_osd_map(self, logger):
        tree = {"nodes": CRUSH_TREE["nodes"] + [
            {"id": 3, "name": "osd.3", "type": "osd", "children": [], "crush_weight": 1.0, "device_class": "hdd"}]}
        result = coral.get_osd_info(make_mock_cluster({"osd crush tree": tree}), logger)
        assert result[3]["in"] == 0 and result[3]["crush_weight"] == 1.0

    def test_display_handles_osd_without_crush_location(self, logger, capsys):
        osd_dump = {**OSD_DUMP, "osds": OSD_DUMP["osds"] + [{"osd": 2, "up": 0, "in": 0}]}
        state = coral.get_cluster_state(make_mock_cluster({"osd dump": osd_dump}), logger)
        coral.calculate_usage(state, logger)
        coral.display_usage(state)
        coral.display_pgs_by_pool(state)
        assert re.search(r"│\s*2\s*│", capsys.readouterr().out)    # osd.2's row in the usage table

    def test_uses_given_pg_dump(self, mock_cluster, logger):
        pg_dump = {"pg_map": {"pg_stats": [], "osd_stats": [{"osd": 0, "kb": 7}, {"osd": 1, "kb": 7}]}}
        result = coral.get_osd_info(mock_cluster, logger, pg_dump)
        assert result[0]["size"] == 7 * 1024
        prefixes = [json.loads(c.args[0])["prefix"] for c in mock_cluster.mon_command.call_args_list]
        assert "pg dump" not in prefixes
        assert "osd df" not in prefixes


class TestGetClusterState:
    def test_fetches_pg_dump_once(self, mock_cluster, logger):
        coral.get_cluster_state(mock_cluster, logger)
        prefixes = [json.loads(c.args[0])["prefix"] for c in mock_cluster.mon_command.call_args_list]
        assert prefixes.count("pg dump") == 1


class TestGetPgInfo:
    def test_up_and_acting_sets(self, mock_cluster, logger):
        result = coral.get_pg_info(mock_cluster, logger)
        assert result["1.0"]["up"] == [1]
        assert result["1.0"]["acting"] == [0]

    def test_num_bytes(self, mock_cluster, logger):
        result = coral.get_pg_info(mock_cluster, logger)
        assert result["1.0"]["num_bytes"] == GiB

    def test_upmaps_parsed_as_tuples(self, mock_cluster, logger):
        result = coral.get_pg_info(mock_cluster, logger)
        assert result["1.0"]["upmaps"] == [(0, 1)]

    def test_pg_without_upmap_has_empty_list(self, logger):
        cluster = make_mock_cluster({"osd dump": {**OSD_DUMP, "pg_upmap_items": []}})
        result = coral.get_pg_info(cluster, logger)
        assert result["1.0"]["upmaps"] == []


class TestGetPoolInfo:
    def test_replica_pool_fields(self, mock_cluster, logger):
        result = coral.get_pool_info(mock_cluster, logger)
        assert result[1]["type"] == "replica"
        assert result[1]["name"] == "rbd"
        assert result[1]["size"] == 3
        assert "k" not in result[1]

    def test_ec_pool_fetches_k_and_m(self, logger):
        cluster = make_mock_cluster({"osd dump": {**OSD_DUMP, "pools": POOLS_EC}})
        result = coral.get_pool_info(cluster, logger)
        assert result[1]["type"] == "erasure"
        assert result[1]["k"] == 4
        assert result[1]["m"] == 2

    def test_reads_pools_from_osd_dump(self, mock_cluster, logger):
        coral.get_pool_info(mock_cluster, logger)
        prefixes = [json.loads(c.args[0])["prefix"] for c in mock_cluster.mon_command.call_args_list]
        assert "osd pool ls" not in prefixes
        assert "osd dump" in prefixes


class TestGetBackfillfullRatio:
    def test_returns_value_from_cluster(self, mock_cluster, logger):
        assert coral.get_backfillfull_ratio(mock_cluster, logger) == 0.9

    def test_defaults_to_0_95_when_missing(self, logger):
        osd_dump = {k: v for k, v in OSD_DUMP.items() if k != "backfillfull_ratio"}
        cluster = make_mock_cluster({"osd dump": osd_dump})
        assert coral.get_backfillfull_ratio(cluster, logger) == 0.95


class TestGetCrushRules:
    def test_indexed_by_rule_id(self, mock_cluster, logger):
        result = coral.get_crush_rules(mock_cluster, logger)
        assert 0 in result
        assert result[0]["name"] == "replicated_rule"

    def test_steps_preserved(self, mock_cluster, logger):
        result = coral.get_crush_rules(mock_cluster, logger)
        assert len(result[0]["steps"]) == 3

    def test_includes_valid_osds(self, mock_cluster, logger):
        result = coral.get_crush_rules(mock_cluster, logger)
        assert result[0]["valid_osds"] == [0, 1]

    def test_resolves_device_class_shadow_buckets(self, logger):
        shadow_tree = {
            "nodes": [
                {"id": -1, "name": "default", "type": "root", "children": [-2, -3]},
                {"id": -2, "name": "host0", "type": "host", "children": [0]},
                {"id": -3, "name": "host1", "type": "host", "children": [1]},
                {"id": 0, "name": "osd.0", "type": "osd", "children": []},
                {"id": 1, "name": "osd.1", "type": "osd", "children": []},
                {"id": -10, "name": "default~hdd", "type": "root", "children": [-11]},
                {"id": -11, "name": "host0~hdd", "type": "host", "children": [0]},
                {"id": -20, "name": "default~ssd", "type": "root", "children": [-21]},
                {"id": -21, "name": "host1~ssd", "type": "host", "children": [1]},
            ]
        }
        rules = [
            {"rule_id": 0, "rule_name": "hdd_rule", "steps": [
                {"op": "take", "item_name": "default~hdd"},
                {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
                {"op": "emit"},
            ]},
            {"rule_id": 1, "rule_name": "ssd_rule", "steps": [
                {"op": "take", "item_name": "default~ssd"},
                {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
                {"op": "emit"},
            ]},
        ]
        cluster = make_mock_cluster({"osd crush tree": shadow_tree, "osd crush rule dump": rules})
        result = coral.get_crush_rules(cluster, logger)
        assert result[0]["valid_osds"] == [0]
        assert result[1]["valid_osds"] == [1]
        logger.warning.assert_not_called()


class TestGetOsdsInBucket:
    nodes = {
        -1: {"id": -1, "name": "default", "type": "root", "children": [-2, -3]},
        -2: {"id": -2, "name": "host0", "type": "host", "children": [0]},
        -3: {"id": -3, "name": "host1", "type": "host", "children": [1]},
        0: {"id": 0, "name": "osd.0", "type": "osd", "children": []},
        1: {"id": 1, "name": "osd.1", "type": "osd", "children": []},
    }

    def test_returns_all_osds_under_root(self):
        assert coral.get_osds_in_bucket(-1, self.nodes) == {0, 1}

    def test_returns_only_osds_under_host(self):
        assert coral.get_osds_in_bucket(-2, self.nodes) == {0}

    def test_returns_self_when_given_an_osd(self):
        assert coral.get_osds_in_bucket(0, self.nodes) == {0}

    def test_returns_empty_for_unknown_id(self):
        assert coral.get_osds_in_bucket(999, self.nodes) == set()


class TestGetOsdBucketMaps:
    def test_maps_osds_to_host_bucket(self, mock_cluster, logger):
        result = coral.get_osd_bucket_maps(mock_cluster, logger)
        assert "host" in result
        assert result["host"][0] == "host0"
        assert result["host"][1] == "host1"

    def test_osd_failure_domain_maps_each_osd_to_itself(self, logger):
        # A rule whose failure domain is the OSD itself must produce a
        # populated bucket map (each OSD as its own unique bucket), not the
        # empty dict that falls out of walking the CRUSH tree for an
        # 'osd'-type ancestor that never exists.
        rules = [
            {"rule_id": 0, "rule_name": "osd_rule", "steps": [
                {"op": "take", "item_name": "default"},
                {"op": "chooseleaf_firstn", "num": 0, "type": "osd"},
                {"op": "emit"},
            ]},
        ]
        cluster = make_mock_cluster({"osd crush rule dump": rules})
        result = coral.get_osd_bucket_maps(cluster, logger)
        assert result["osd"] == {0: "osd.0", 1: "osd.1"}

    def test_ignores_device_class_shadow_buckets(self, logger):
        # Shape of real 'osd crush tree --show-shadow' output: shadow buckets
        # come after the real ones and each OSD is listed once per tree. The
        # shadow host would otherwise overwrite the OSD's real parent and the
        # failure domain would come back as 'host0~hdd' instead of 'host0'.
        shadow_tree = {
            "nodes": [
                {"id": -1, "name": "default", "type": "root", "children": [-2, -3]},
                {"id": -2, "name": "host0", "type": "host", "children": [0]},
                {"id": 0, "name": "osd.0", "type": "osd", "children": []},
                {"id": -3, "name": "host1", "type": "host", "children": [1]},
                {"id": 1, "name": "osd.1", "type": "osd", "children": []},
                {"id": -10, "name": "default~hdd", "type": "root", "children": [-11, -12]},
                {"id": -11, "name": "host0~hdd", "type": "host", "children": [0]},
                {"id": 0, "name": "osd.0", "type": "osd", "children": []},
                {"id": -12, "name": "host1~hdd", "type": "host", "children": [1]},
                {"id": 1, "name": "osd.1", "type": "osd", "children": []},
            ]
        }
        cluster = make_mock_cluster({"osd crush tree": shadow_tree})
        result = coral.get_osd_bucket_maps(cluster, logger)
        assert result["host"] == {0: "host0", 1: "host1"}

    def test_requests_shadow_tree(self, mock_cluster, logger):
        coral.get_osd_bucket_maps(mock_cluster, logger)
        tree_cmds = [json.loads(c.args[0]) for c in mock_cluster.mon_command.call_args_list
                     if json.loads(c.args[0])["prefix"] == "osd crush tree"]
        assert tree_cmds and all(c.get("shadow") == "--show-shadow" for c in tree_cmds)


class TestCalculatePgDistribution:
    @staticmethod
    def _state(osd_weights, pools, valid_osds, in_status=None):
        in_status = in_status or {}
        return {
            "osds": {
                osd_id: {"crush_weight": w, "in": in_status.get(osd_id, 1)}
                for osd_id, w in osd_weights.items()
            },
            "pools": pools,
            "crush_rules": {0: {"name": "r", "steps": [], "valid_osds": valid_osds}},
        }

    def test_equal_weights_split_evenly(self, logger):
        state = self._state(
            {0: 1.0, 1: 1.0},
            {1: {"crush_rule": 0, "pgs": 128, "size": 3}},
            valid_osds=[0, 1],
        )
        coral.calculate_pg_distribution(state, logger)
        # 0.5 * 128 * 3 = 192
        assert state["osds"][0]["target_pgs_by_pool"][1] == (192, 192)
        assert state["osds"][1]["target_pgs_by_pool"][1] == (192, 192)

    def test_uneven_division_floors_and_ceils(self, logger):
        state = self._state(
            {0: 1.0, 1: 1.0, 2: 1.0},
            {1: {"crush_rule": 0, "pgs": 100, "size": 1}},
            valid_osds=[0, 1, 2],
        )
        coral.calculate_pg_distribution(state, logger)
        # (1/3) * 100 * 1 = 33.333… → (33, 34)
        for osd_id in [0, 1, 2]:
            assert state["osds"][osd_id]["target_pgs_by_pool"][1] == (33, 34)

    def test_zero_weight_osd_gets_zero_target(self, logger):
        state = self._state(
            {0: 1.0, 1: 1.0, 2: 0.0},
            {1: {"crush_rule": 0, "pgs": 128, "size": 3}},
            valid_osds=[0, 1, 2],
        )
        coral.calculate_pg_distribution(state, logger)
        assert state["osds"][2]["target_pgs_by_pool"][1] == (0, 0)
        # The two real OSDs split the full 384 PG·replicas
        assert state["osds"][0]["target_pgs_by_pool"][1] == (192, 192)
        assert state["osds"][1]["target_pgs_by_pool"][1] == (192, 192)

    def test_tiny_weight_treated_as_zero(self, logger):
        state = self._state(
            {0: 1.0, 1: 0.00005},  # below 0.0001 threshold
            {1: {"crush_rule": 0, "pgs": 128, "size": 3}},
            valid_osds=[0, 1],
        )
        coral.calculate_pg_distribution(state, logger)
        assert state["osds"][1]["target_pgs_by_pool"][1] == (0, 0)
        assert state["osds"][0]["target_pgs_by_pool"][1] == (384, 384)

    def test_ec_pool_uses_size_as_k_plus_m(self, logger):
        state = self._state(
            {0: 1.0, 1: 1.0},
            {1: {"crush_rule": 0, "pgs": 64, "size": 11}},  # 8+3 EC
            valid_osds=[0, 1],
        )
        coral.calculate_pg_distribution(state, logger)
        # 0.5 * 64 * 11 = 352
        assert state["osds"][0]["target_pgs_by_pool"][1] == (352, 352)

    def test_osd_not_in_valid_osds_has_no_entry_for_that_pool(self, logger):
        state = self._state(
            {0: 1.0, 1: 1.0, 2: 1.0},
            {1: {"crush_rule": 0, "pgs": 128, "size": 3}},
            valid_osds=[0, 1],
        )
        coral.calculate_pg_distribution(state, logger)
        assert 1 not in state["osds"][2]["target_pgs_by_pool"]

    def test_all_zero_weights_yields_zero_targets(self, logger):
        state = self._state(
            {0: 0.0, 1: 0.0},
            {1: {"crush_rule": 0, "pgs": 128, "size": 3}},
            valid_osds=[0, 1],
        )
        coral.calculate_pg_distribution(state, logger)
        assert state["osds"][0]["target_pgs_by_pool"][1] == (0, 0)
        assert state["osds"][1]["target_pgs_by_pool"][1] == (0, 0)

    def test_out_osd_gets_zero_target_and_doesnt_pull_share(self, logger):
        # OSD 2 is OUT — its weight must not be counted in total_weight, so
        # OSDs 0 and 1 split the full pool share between themselves.
        state = self._state(
            {0: 1.0, 1: 1.0, 2: 1.0},
            {1: {"crush_rule": 0, "pgs": 128, "size": 3}},
            valid_osds=[0, 1, 2],
            in_status={0: 1, 1: 1, 2: 0},
        )
        coral.calculate_pg_distribution(state, logger)
        assert state["osds"][2]["target_pgs_by_pool"][1] == (0, 0)
        assert state["osds"][0]["target_pgs_by_pool"][1] == (192, 192)
        assert state["osds"][1]["target_pgs_by_pool"][1] == (192, 192)

    def test_invoked_by_get_cluster_state(self, mock_cluster, logger):
        state = coral.get_cluster_state(mock_cluster, logger)
        # Fixture: 2 equal-weight OSDs, pool 1 with 128 PGs, size 3
        assert state["osds"][0]["target_pgs_by_pool"][1] == (192, 192)


class TestCalculateUsage:
    def test_acting_set_populates_current_usage(self, base_cluster_state, logger):
        coral.calculate_usage(base_cluster_state, logger)
        assert base_cluster_state["osds"][0]["current_usage"] == GiB
        assert base_cluster_state["osds"][0]["current_pgs"] == 1
        assert base_cluster_state["osds"][0]["current_pgs_by_pool"][1] == 1

    def test_up_set_populates_future_usage(self, base_cluster_state, logger):
        coral.calculate_usage(base_cluster_state, logger)
        assert base_cluster_state["osds"][1]["future_usage"] == GiB
        assert base_cluster_state["osds"][1]["future_pgs"] == 1
        assert base_cluster_state["osds"][1]["future_pgs_by_pool"][1] == 1

    def test_ec_pool_divides_bytes_by_k(self, logger):
        state = {
            "osds": {
                0: {"size": 10 * GiB, "current_usage": 0, "current_pgs": 0, "future_usage": 0, "future_pgs": 0},
            },
            "pgs": {
                "2.0": {"up": [], "acting": [0], "state": ["active"], "num_bytes": 4 * GiB, "upmaps": []}
            },
            "pools": {
                2: {"name": "ecpool", "type": "erasure", "crush_rule": 0, "pgs": 1, "size": 6, "k": 4, "m": 2}
            },
            "backfillfull_ratio": 0.9,
            "upmap_queue": {},
        }
        coral.calculate_usage(state, logger)
        assert state["osds"][0]["current_usage"] == GiB  # 4 GiB / k=4

    def test_crush_item_none_is_skipped(self, logger):
        state = {
            "osds": {
                0: {"size": 10 * GiB, "current_usage": 0, "current_pgs": 0, "future_usage": 0, "future_pgs": 0},
            },
            "pgs": {
                "1.0": {
                    "up": [coral.CRUSH_ITEM_NONE],
                    "acting": [coral.CRUSH_ITEM_NONE],
                    "state": ["active"],
                    "num_bytes": GiB,
                    "upmaps": [],
                }
            },
            "pools": {
                1: {"name": "rbd", "type": "replica", "crush_rule": 0, "pgs": 1, "size": 1}
            },
            "backfillfull_ratio": 0.9,
            "upmap_queue": {},
        }
        coral.calculate_usage(state, logger)
        assert state["osds"][0]["current_usage"] == 0
        assert state["osds"][0]["future_usage"] == 0


class TestQueueUpmapMappingRemoval:
    def test_removes_mapping_from_pg_upmaps(self, base_cluster_state, logger):
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert (0, 1) not in base_cluster_state["pgs"]["1.0"]["upmaps"]

    def test_adds_bytes_back_to_from_osd(self, base_cluster_state, logger):
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert base_cluster_state["osds"][0]["future_usage"] == GiB

    def test_subtracts_bytes_from_to_osd(self, base_cluster_state, logger):
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert base_cluster_state["osds"][1]["future_usage"] == 0

    def test_shifts_per_pool_pg_counts(self, base_cluster_state, logger):
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert base_cluster_state["osds"][0]["future_pgs_by_pool"][1] == 1
        assert base_cluster_state["osds"][1]["future_pgs_by_pool"][1] == 0

    def test_enqueues_pgid(self, base_cluster_state, logger):
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert "1.0" in base_cluster_state["upmap_queue"]

    def test_restores_from_osd_in_up_set(self, base_cluster_state, logger):
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert base_cluster_state["pgs"]["1.0"]["up"] == [0]

    def test_restores_from_osd_in_same_up_slot(self, logger):
        # EC shard position matters: from_osd must return to to_osd's slot.
        state = {
            "osds": {i: {"future_usage": 0, "future_pgs": 1, "future_pgs_by_pool": {1: 1}} for i in range(4)},
            "pgs": {"1.0": {"up": [2, 3, 1], "acting": [2, 3, 1], "num_bytes": 0, "upmaps": [(0, 3)]}},
            "pools": {1: {"type": "replica"}},
            "upmap_queue": {},
        }
        coral.queue_upmap_mapping_removal(state, "1.0", (0, 3), logger)
        assert state["pgs"]["1.0"]["up"] == [2, 0, 1]

    def test_ec_pool_shifts_per_shard_bytes_not_full_pg(self, logger):
        # EC pool with k=4: each OSD's shard is num_bytes / 4, not num_bytes.
        state = {
            "osds": {
                0: {"size": 10 * GiB, "future_usage": 0, "future_pgs": 0, "future_pgs_by_pool": {2: 0}},
                1: {"size": 10 * GiB, "future_usage": GiB, "future_pgs": 1, "future_pgs_by_pool": {2: 1}},
            },
            "pgs": {
                "2.0": {"up": [1], "acting": [0], "num_bytes": 4 * GiB, "upmaps": [(0, 1)]}
            },
            "pools": {
                2: {"name": "ecpool", "type": "erasure", "crush_rule": 0, "pgs": 1, "size": 6, "k": 4, "m": 2}
            },
            "upmap_queue": {},
        }
        coral.queue_upmap_mapping_removal(state, "2.0", (0, 1), logger)
        assert state["osds"][0]["future_usage"] == GiB  # 4 GiB / k=4
        assert state["osds"][1]["future_usage"] == 0


def make_state(targets, pgs, ins=None):
    """Single replicated pool 1, one host per OSD, counts filled in by calculate_usage().

    targets: {osd: (floor, ceil)}; pgs: {pgid: (up, upmap_items)}.
    """
    state = {
        "osds": {o: {"in": (ins or {}).get(o, 1), "size": 10 * GiB, "target_pgs_by_pool": {1: t}}
                 for o, t in targets.items()},
        "pgs": {pgid: {"up": list(up), "acting": list(up), "num_bytes": GiB, "upmaps": list(items)}
                for pgid, (up, items) in pgs.items()},
        "pools": {1: {"name": "p", "type": "replica", "crush_rule": 0, "pgs": len(pgs),
                      "size": len(next(iter(pgs.values()))[0])}},
        "crush_rules": {0: {"name": "r", "valid_osds": sorted(targets),
                            "steps": [{"op": "take", "item_name": "default"},
                                      {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
                                      {"op": "emit"}]}},
        "osd_bucket_maps": {"host": {o: f"host{o}" for o in targets}},
        "upmap_queue": {},
    }
    coral.calculate_usage(state, MagicMock())
    return state


def pool_counts(state):
    return {o: state["osds"][o]["future_pgs_by_pool"].get(1, 0) for o in state["osds"]}


class TestRebuildUpmaps:
    def test_recovers_crush_placement_through_chained_items(self, logger):
        # CRUSH gave [2, 1, 0]; (2 -> 3) then (1 -> 2) turned it into [3, 2, 0].
        # Every OSD is in range without upmaps, so nothing is worth keeping.
        state = make_state({0: (1, 1), 1: (1, 1), 2: (1, 1), 3: (0, 1)},
                           {"1.0": ([3, 2, 0], [(2, 3), (1, 2)])})
        coral.rebuild_upmaps(state, logger)
        assert state["pgs"]["1.0"]["up"] == [2, 1, 0]
        assert state["pgs"]["1.0"]["upmaps"] == []
        assert state["upmap_queue"] == {"1.0": []}
        assert pool_counts(state) == {0: 1, 1: 1, 2: 1, 3: 0}

    def test_drops_item_ceph_never_applied(self, logger):
        # (1 -> 3) targets an out OSD, so Ceph skipped it: up is CRUSH's [0, 1].
        state = make_state({0: (1, 1), 1: (1, 1), 3: (0, 0)},
                           {"1.0": ([0, 1], [(1, 3)])}, ins={3: 0})
        coral.rebuild_upmaps(state, logger)
        assert state["pgs"]["1.0"]["up"] == [0, 1]
        assert state["pgs"]["1.0"]["upmaps"] == []
        assert pool_counts(state) == {0: 1, 1: 1, 3: 0}

    def test_keeps_helpful_upmap_and_does_not_resend_it(self, logger):
        # Without upmaps OSD 2 would be empty (under its floor of 1); the
        # existing (0 -> 2) fixes that, so it stays — and since the PG ends up
        # with exactly the items it already has, it isn't part of the plan.
        state = make_state({0: (1, 2), 1: (1, 2), 2: (1, 2)},
                           {"1.0": ([2, 1], [(0, 2)]), "1.1": ([0, 1], [])})
        coral.rebuild_upmaps(state, logger)
        assert state["pgs"]["1.0"]["upmaps"] == [(0, 2)]
        assert state["pgs"]["1.0"]["up"] == [2, 1]
        assert coral.build_upmap_plan(state) == {}

    def test_drops_upmap_that_would_leave_source_under_floor(self, logger):
        # OSD 0 needs both of its CRUSH PGs (floor 2), so (0 -> 2) is dropped
        # and PG 1.0 goes back to OSD 0.
        state = make_state({0: (2, 2), 1: (2, 2), 2: (0, 0)},
                           {"1.0": ([2, 1], [(0, 2)]), "1.1": ([0, 1], [])})
        coral.rebuild_upmaps(state, logger)
        assert state["pgs"]["1.0"]["upmaps"] == []
        assert state["pgs"]["1.0"]["up"] == [0, 1]
        assert state["upmap_queue"] == {"1.0": []}

    def test_does_not_keep_upmaps_moving_data_onto_and_off_one_osd(self, logger):
        # Existing upmaps (0 -> 2) on 1.0 and (2 -> 4) on 1.1 shuffle data
        # through OSD 2. The rebuild keeps (0 -> 2), refuses (2 -> 4), and the
        # balancer then fills OSD 4 by collapsing 1.0's chain into (0 -> 4).
        state = make_state({0: (0, 0), 1: (1, 1), 2: (1, 2), 3: (1, 1), 4: (1, 1)},
                           {"1.0": ([2, 1], [(0, 2)]), "1.1": ([4, 3], [(2, 4)])})
        coral.rebuild_upmaps(state, logger)
        assert state["pgs"]["1.0"]["upmaps"] == [(0, 2)]
        assert state["pgs"]["1.1"]["upmaps"] == []
        coral.balance_off_target_osds(state, logger)
        assert state["pgs"]["1.0"]["upmaps"] == [(0, 4)]
        assert state["pgs"]["1.1"]["upmaps"] == []
        assert pool_counts(state) == {0: 0, 1: 1, 2: 1, 3: 1, 4: 1}

    def test_balance_after_rebuild_sees_restored_up_set(self, logger):
        # PG 1.0's CRUSH set is [0, 2], redirected to [1, 2] by upmap (0 -> 1).
        # Keeping it would leave osd.0 under its floor, so it's dropped. The
        # balancer must then plan against up=[0, 2]; with a stale up=[1, 2] it
        # would queue a bogus (2 -> 0) that Ceph ignores because osd.0 is
        # already in the PG.
        osds = {i: {"in": 1, "crush_weight": 1.0} for i in range(3)}
        pgs = {
            "1.0": {"up": [1, 2], "acting": [1, 2], "num_bytes": 1, "upmaps": [(0, 1)]},
            "1.1": {"up": [1, 2], "acting": [1, 2], "num_bytes": 1, "upmaps": []},
            "1.2": {"up": [1, 2], "acting": [1, 2], "num_bytes": 1, "upmaps": []},
        }
        state = {
            "osds": osds,
            "pgs": pgs,
            "pools": {1: {"type": "replica", "crush_rule": 0, "pgs": 3, "size": 2}},
            "crush_rules": {0: {"steps": [{"op": "chooseleaf_firstn", "type": "host"}], "valid_osds": [0, 1, 2]}},
            "osd_bucket_maps": {"host": {0: "h0", 1: "h1", 2: "h2"}},
            "upmap_queue": {},
        }
        coral.calculate_pg_distribution(state, logger)
        coral.calculate_usage(state, logger)
        coral.rebuild_upmaps(state, logger)
        coral.balance_off_target_osds(state, logger)

        for pgid, pg in pgs.items():
            assert len(set(pg["up"])) == len(pg["up"]), pgid
            for from_osd, to_osd in pg["upmaps"]:
                assert from_osd != to_osd
        # Every OSD lands at its target of 2 PGs, and the model matches the
        # up sets the balancer left behind.
        for osd_id in osds:
            actual = sum(osd_id in pg["up"] for pg in pgs.values())
            assert osds[osd_id]["future_pgs_by_pool"][1] == actual == 2


class TestQueueUpmapMappingAddition:
    def test_appends_mapping_to_pg_upmaps(self, balance_cluster_state, logger):
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert (0, 2) in balance_cluster_state["pgs"]["1.0"]["upmaps"]

    def test_replaces_from_osd_in_up_set(self, balance_cluster_state, logger):
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert balance_cluster_state["pgs"]["1.0"]["up"] == [2, 1]

    def test_shifts_byte_counters(self, balance_cluster_state, logger):
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert balance_cluster_state["osds"][0]["future_usage"] == 0
        assert balance_cluster_state["osds"][2]["future_usage"] == GiB

    def test_shifts_pg_count_counters(self, balance_cluster_state, logger):
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert balance_cluster_state["osds"][0]["future_pgs"] == 0
        assert balance_cluster_state["osds"][2]["future_pgs"] == 1

    def test_shifts_per_pool_pg_counts(self, balance_cluster_state, logger):
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert balance_cluster_state["osds"][0]["future_pgs_by_pool"][1] == 0
        assert balance_cluster_state["osds"][2]["future_pgs_by_pool"][1] == 1

    def test_enqueues_pgid(self, balance_cluster_state, logger):
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert "1.0" in balance_cluster_state["upmap_queue"]

    def test_ec_pool_shifts_per_shard_bytes_not_full_pg(self, logger):
        # EC pool with k=4: each OSD's shard is num_bytes / 4, not num_bytes.
        state = {
            "osds": {
                0: {"size": 10 * GiB, "future_usage": GiB, "future_pgs": 1, "future_pgs_by_pool": {2: 1}},
                1: {"size": 10 * GiB, "future_usage": 0, "future_pgs": 0, "future_pgs_by_pool": {2: 0}},
            },
            "pgs": {
                "2.0": {"up": [0], "acting": [0], "num_bytes": 4 * GiB, "upmaps": []}
            },
            "pools": {
                2: {"name": "ecpool", "type": "erasure", "crush_rule": 0, "pgs": 1, "size": 6, "k": 4, "m": 2}
            },
            "upmap_queue": {},
        }
        coral.queue_upmap_mapping_addition(state, "2.0", (0, 1), logger)
        assert state["osds"][0]["future_usage"] == 0
        assert state["osds"][1]["future_usage"] == GiB  # 4 GiB / k=4

    def test_collapses_chain_when_from_osd_is_existing_destination(self, balance_cluster_state, logger):
        # Existing upmap (1, 0) redirects to OSD 0. Adding (0, 2) on the same
        # PG would produce the invalid set [(1, 0), (0, 2)] — OSD 0 is both a
        # destination and a source. The helper must collapse the chain into
        # the single upmap (1, 2).
        balance_cluster_state["pgs"]["1.0"]["upmaps"] = [(1, 0)]
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert balance_cluster_state["pgs"]["1.0"]["upmaps"] == [(1, 2)]

    def test_collapses_chain_to_noop_when_round_trip(self, balance_cluster_state, logger):
        # Chain 2 -> 0 -> 2 round-trips; the upmap should be dropped entirely
        # rather than written as the self-loop (2, 2).
        balance_cluster_state["pgs"]["1.0"]["upmaps"] = [(2, 0)]
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert balance_cluster_state["pgs"]["1.0"]["upmaps"] == []

    def test_unrelated_existing_upmaps_are_preserved(self, balance_cluster_state, logger):
        # An existing upmap whose destination is NOT the new from_osd should
        # remain untouched alongside the newly appended mapping.
        balance_cluster_state["pgs"]["1.0"]["upmaps"] = [(1, 99)]
        coral.queue_upmap_mapping_addition(balance_cluster_state, "1.0", (0, 2), logger)
        assert (1, 99) in balance_cluster_state["pgs"]["1.0"]["upmaps"]
        assert (0, 2) in balance_cluster_state["pgs"]["1.0"]["upmaps"]


class TestBalanceOffTargetOsds:
    def test_queues_upmap_when_osd_over_and_destination_available(self, balance_cluster_state, logger):
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"]["1.0"] == [(0, 2)]

    def test_updates_counters_after_move(self, balance_cluster_state, logger):
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["osds"][0]["future_pgs_by_pool"][1] == 0
        assert balance_cluster_state["osds"][2]["future_pgs_by_pool"][1] == 1

    def test_no_op_when_all_osds_on_target(self, balance_cluster_state, logger):
        # Widen OSD 0's ceil so its count of 1 is on-target
        balance_cluster_state["osds"][0]["target_pgs_by_pool"][1] = (0, 1)
        balance_cluster_state["osds"][2]["target_pgs_by_pool"][1] = (0, 0)
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_no_op_when_no_under_target_osd(self, balance_cluster_state, logger):
        balance_cluster_state["osds"][2]["target_pgs_by_pool"][1] = (0, 0)
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_skips_destination_in_same_failure_domain(self, balance_cluster_state, logger):
        # Put OSD 2 in host1 — same bucket as OSD 1, which is in the up set
        balance_cluster_state["osd_bucket_maps"]["host"][2] = "host1"
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_skips_destination_outside_valid_osds(self, balance_cluster_state, logger):
        balance_cluster_state["crush_rules"][0]["valid_osds"] = [0, 1]
        # OSD 2 still has a target entry but the rule rejects it
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_skips_destination_already_in_up_set(self, balance_cluster_state, logger):
        # Make OSD 1 under-target and OSD 2 over-target; OSD 1 is already in the
        # PG's up set so it cannot receive the upmap, and no other candidate
        # exists — function should make no move.
        balance_cluster_state["osds"][1]["target_pgs_by_pool"][1] = (2, 2)
        balance_cluster_state["osds"][1]["future_pgs_by_pool"][1] = 1  # below floor
        balance_cluster_state["osds"][2]["target_pgs_by_pool"][1] = (0, 0)
        balance_cluster_state["osds"][2]["future_pgs_by_pool"][1] = 1  # above ceil (synthetic)
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_terminates_when_no_valid_move_exists(self, balance_cluster_state, logger):
        # Force OSD 0 over and OSD 2 under, but block OSD 2 via failure domain.
        # If the function failed to terminate it would hang the test runner.
        balance_cluster_state["osd_bucket_maps"]["host"][2] = "host1"
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_drained_osd_hands_off_through_at_ceil_osd(self, balance_cluster_state, logger):
        # OSD 0 is being drained (target (0, 0)) and holds a replica of PG 1.0.
        # The only OSD with room below its ceil (3) shares host1 with OSD 1,
        # which holds the PG's other replica, so a direct move is blocked.
        # OSD 2 is at its ceil but on a free host: the PG has to go there, and
        # then OSD 2 (now over-ceil) passes PG 1.1 on to OSD 3. Without the
        # drain fallback the balancer stops and OSD 0 keeps its PG.
        state = balance_cluster_state

        def osd(count, target):
            return {"size": 10 * GiB, "current_usage": count * GiB, "current_pgs": count,
                    "current_pgs_by_pool": {1: count}, "future_usage": count * GiB,
                    "future_pgs": count, "future_pgs_by_pool": {1: count},
                    "target_pgs_by_pool": {1: target}}

        state["osds"] = {0: osd(1, (0, 0)), 1: osd(1, (1, 1)), 2: osd(1, (1, 1)),
                         3: osd(0, (1, 1)), 4: osd(1, (1, 1))}
        state["pgs"] = {
            "1.0": {"up": [0, 1], "acting": [0, 1], "state": ["active", "clean"], "num_bytes": GiB, "upmaps": []},
            "1.1": {"up": [2, 4], "acting": [2, 4], "state": ["active", "clean"], "num_bytes": GiB, "upmaps": []},
        }
        state["pools"][1]["pgs"] = 2
        state["crush_rules"][0]["valid_osds"] = [0, 1, 2, 3, 4]
        state["osd_bucket_maps"]["host"] = {0: "host0", 1: "host1", 2: "host2", 3: "host1", 4: "host4"}

        coral.balance_off_target_osds(state, logger)

        assert state["upmap_queue"] == {"1.0": [(0, 2)], "1.1": [(2, 3)]}
        assert {o: state["osds"][o]["future_pgs_by_pool"][1] for o in state["osds"]} == \
            {0: 0, 1: 1, 2: 1, 3: 1, 4: 1}

    def test_drain_fallback_leaves_pg_when_every_destination_is_blocked(self, balance_cluster_state, logger):
        # Same drained OSD 0, but the only other OSDs share host1 with OSD 1 —
        # there's nowhere legal to put the replica, so nothing is queued.
        state = balance_cluster_state
        state["osds"][2]["future_pgs_by_pool"][1] = 1
        state["osds"][2]["future_pgs"] = 1
        state["osd_bucket_maps"]["host"][2] = "host1"
        coral.balance_off_target_osds(state, logger)
        assert state["upmap_queue"] == {}
        assert state["osds"][0]["future_pgs_by_pool"][1] == 1

    def test_pool_with_osd_failure_domain_can_balance(self, balance_cluster_state, logger):
        # Pool's CRUSH rule has an OSD-level failure domain. Two OSDs sharing
        # a host is fine — every OSD is its own bucket. Without the fix in
        # get_osd_bucket_maps, the bucket_map would be empty and every
        # candidate destination would be rejected as a failure-domain collision.
        balance_cluster_state["crush_rules"][0]["steps"] = [
            {"op": "take", "item_name": "default"},
            {"op": "chooseleaf_firstn", "num": 0, "type": "osd"},
            {"op": "emit"},
        ]
        balance_cluster_state["osd_bucket_maps"] = {
            "osd": {0: "osd.0", 1: "osd.1", 2: "osd.2"},
        }
        # Put OSDs 1 and 2 in the same host to prove the host-level domain
        # would have blocked this move — the osd-level domain shouldn't.
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"]["1.0"] == [(0, 2)]

    def test_at_ceil_osd_donates_to_under_target_recipient(self, balance_cluster_state, logger):
        # No OSD is over its ceil. OSD 0 is at-ceil (count=1, target (0,1))
        # with a PG to spare since count > floor. OSD 1 is at-floor (count=1,
        # target (1,2)) so it cannot donate. OSD 2 is under (count=0, target
        # (1,1)). The function must treat at-ceil OSDs as donors when their
        # count is strictly above floor — without that, no donor is found and
        # the under-target OSD never receives a PG.
        balance_cluster_state["osds"][0]["target_pgs_by_pool"][1] = (0, 1)  # count=1, at ceil → donor
        balance_cluster_state["osds"][1]["target_pgs_by_pool"][1] = (1, 2)  # count=1, at floor → NOT donor
        balance_cluster_state["osds"][2]["target_pgs_by_pool"][1] = (1, 1)  # count=0, under   → recipient
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"]["1.0"] == [(0, 2)]
        assert balance_cluster_state["osds"][0]["future_pgs_by_pool"][1] == 0
        assert balance_cluster_state["osds"][2]["future_pgs_by_pool"][1] == 1

    def test_over_ceil_osd_donates_to_at_floor_recipient(self, balance_cluster_state, logger):
        # OSD 0 is OVER ceil (count=2, target (0,1)). OSD 1 is at-floor (count=1,
        # target (1,2)) so it's NOT under-floor, but receiving a PG keeps it at
        # ceil. With the old recipient criterion (count < floor), no recipient
        # would be found and the over-ceil OSD would stay over. The function
        # must allow at-floor (room below ceil) recipients when the donor is
        # itself over-ceil.
        balance_cluster_state["osds"][0]["target_pgs_by_pool"][1] = (0, 1)
        balance_cluster_state["osds"][0]["future_pgs_by_pool"][1] = 2  # over ceil
        balance_cluster_state["osds"][1]["target_pgs_by_pool"][1] = (1, 2)
        balance_cluster_state["osds"][1]["future_pgs_by_pool"][1] = 1  # at floor, room below ceil
        balance_cluster_state["osds"][2]["target_pgs_by_pool"][1] = (0, 0)  # zero target — not a recipient
        # OSD 1 is already in pg.up so move from 0 must land on a different OSD.
        # Adjust the PG so OSD 2 is the only valid destination.
        balance_cluster_state["pgs"]["1.0"]["up"] = [0]
        balance_cluster_state["osds"][2]["target_pgs_by_pool"][1] = (1, 2)  # now a valid recipient (room=2)
        balance_cluster_state["osds"][2]["future_pgs_by_pool"][1] = 1       # at floor

        coral.balance_off_target_osds(balance_cluster_state, logger)
        # Move from over-ceil OSD 0 to at-floor OSD 2 (donor IS over, so the
        # at-floor recipient is allowed).
        assert "1.0" in balance_cluster_state["upmap_queue"]
        assert balance_cluster_state["osds"][0]["future_pgs_by_pool"][1] == 1
        assert balance_cluster_state["osds"][2]["future_pgs_by_pool"][1] == 2

    def test_does_not_shuffle_when_everyone_in_range(self, balance_cluster_state, logger):
        # Both endpoints in range (floor <= count <= ceil) should NOT trigger a
        # move — that would just churn upmaps. OSD 0 at-ceil (count=1, (0,1))
        # and OSD 2 at-floor (count=0, (0,1)) are both in range; no over-ceil
        # and no under-floor OSD exists, so we should make zero moves.
        balance_cluster_state["osds"][0]["target_pgs_by_pool"][1] = (0, 1)
        balance_cluster_state["osds"][1]["target_pgs_by_pool"][1] = (0, 1)
        balance_cluster_state["osds"][2]["target_pgs_by_pool"][1] = (0, 1)
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_falls_back_to_next_over_target_osd_when_top_cannot_move(self, logger):
        # OSD 0 is the most over-target (excess 2) but its only PG (1.0) can't
        # move to OSD 2 — OSD 2 shares OSD 1's bucket, blocking the placement.
        # OSD 3 is also over (excess 1); its PG 1.1 has a valid destination on
        # OSD 2. The function must try OSD 3 as a source after OSD 0 fails.
        state = {
            "osds": {
                osd_id: {
                    "size": 10 * GiB,
                    "current_usage": 0, "current_pgs": 0, "current_pgs_by_pool": {1: 0},
                    "future_usage": 0, "future_pgs": 0, "future_pgs_by_pool": {1: 0},
                    "target_pgs_by_pool": {1: (0, 0)},
                } for osd_id in (0, 1, 2, 3, 4)
            },
            "pgs": {
                "1.0": {"up": [0, 1], "acting": [0, 1], "state": ["active"], "num_bytes": GiB, "upmaps": []},
                "1.1": {"up": [3, 4], "acting": [3, 4], "state": ["active"], "num_bytes": GiB, "upmaps": []},
            },
            "pools": {1: {"name": "rbd", "type": "replica", "crush_rule": 0, "pgs": 2, "size": 2}},
            "crush_rules": {0: {
                "name": "r", "valid_osds": [0, 1, 2, 3, 4],
                "steps": [{"op": "take", "item_name": "default"},
                          {"op": "chooseleaf_firstn", "num": 0, "type": "host"},
                          {"op": "emit"}],
            }},
            "osd_bucket_maps": {"host": {
                0: "host0", 1: "host1", 2: "host1",  # OSD 2 shares OSD 1's host -> blocks PG 1.0
                3: "host2", 4: "host3",
            }},
            "upmap_queue": {},
        }
        # OSD 0 is most-over (synthetic count=3 vs target (1, 1)); OSDs 1, 3, 4 each have one PG;
        # OSD 2 is under by 1. The first iteration tries OSD 0, fails, must try OSD 3.
        # OSD 0's target is non-zero so it isn't a drained OSD — otherwise the
        # drain fallback (drain_to_at_ceil_osd) would legitimately move 1.0 to OSD 4.
        state["osds"][0]["target_pgs_by_pool"][1] = (1, 1)
        state["osds"][0]["future_pgs_by_pool"][1] = 3
        state["osds"][1]["target_pgs_by_pool"][1] = (1, 1)
        state["osds"][1]["future_pgs_by_pool"][1] = 1
        state["osds"][3]["future_pgs_by_pool"][1] = 1
        state["osds"][4]["target_pgs_by_pool"][1] = (1, 1)
        state["osds"][4]["future_pgs_by_pool"][1] = 1
        state["osds"][2]["target_pgs_by_pool"][1] = (1, 1)

        coral.balance_off_target_osds(state, logger)
        assert "1.1" in state["upmap_queue"]
        assert state["upmap_queue"]["1.1"] == [(3, 2)]
        assert "1.0" not in state["upmap_queue"]


class TestSendUpmapItems:
    @staticmethod
    def _mock_cluster(ret=0):
        cluster = MagicMock()
        cluster.mon_command.return_value = (ret, b'', 'error' if ret else '')
        return cluster

    def test_pg_upmap_items_command_sends_integer_ids(self, logger):
        cluster = self._mock_cluster()
        assert coral.send_upmap_items(cluster, "1.0", [(0, 1)], False, logger)
        cmd = json.loads(cluster.mon_command.call_args.args[0])
        assert cmd == {"prefix": "osd pg-upmap-items", "pgid": "1.0", "id": [0, 1], "format": "json"}
        assert all(isinstance(i, int) for i in cmd["id"])

    def test_rm_pg_upmap_items_command_for_empty_mapping(self, logger):
        cluster = self._mock_cluster()
        coral.send_upmap_items(cluster, "1.0", [], False, logger)
        cmd = json.loads(cluster.mon_command.call_args.args[0])
        assert cmd == {"prefix": "osd rm-pg-upmap-items", "pgid": "1.0", "format": "json"}

    def test_dryrun_prints_string_form(self, logger, capsys):
        cluster = self._mock_cluster()
        coral.send_upmap_items(cluster, "1.0", [(0, 1), (2, 3)], True, logger)
        assert "ceph osd pg-upmap-items 1.0 0 1 2 3" in capsys.readouterr().out
        cluster.mon_command.assert_not_called()

    def test_reports_failure(self, logger):
        assert not coral.send_upmap_items(self._mock_cluster(ret=-22), "1.0", [(0, 1)], False, logger)
        logger.error.assert_called_once()


def all_in(osd_id):
    return True


class TestUpmapItemHelpers:
    def test_apply_matches_ceph_rules(self):
        # (5 -> 6) skipped: 6 is already in the set; (9 -> 7) skipped: 9 isn't
        assert coral.apply_upmap_items([1, 2, 6], [(1, 3), (5, 6), (9, 7)], all_in) == [3, 2, 6]
        # target marked out: skipped
        assert coral.apply_upmap_items([1, 2], [(1, 3)], lambda o: o != 3) == [1, 2]

    def test_unapply_reverses_a_chain(self):
        # CRUSH [2, 1, 0] -> (2 -> 3) -> [3, 1, 0] -> (1 -> 2) -> [3, 2, 0]
        assert coral.unapply_upmap_items([3, 2, 0], [(2, 3), (1, 2)], all_in) == [2, 1, 0]

    def test_items_for_up_orders_items_so_each_applies(self):
        # Position 1 needs 0 (CRUSH's OSD at position 0), so position 0 must move first
        items = coral.items_for_up([0, 1], [5, 0], all_in)
        assert items == [(0, 5), (1, 0)]
        assert coral.apply_upmap_items([0, 1], items, all_in) == [5, 0]

    def test_items_for_up_refuses_a_swap(self):
        assert coral.items_for_up([0, 1], [1, 0], all_in) is None

    def test_next_step_changes_one_shard(self):
        # 3-shard EC PG moving every shard: the first step moves only shard 0
        raw, target = [0, 1, 2], [3, 4, 5]
        items = coral.next_upmap_step(raw, [0, 1, 2], target, all_in)
        assert coral.apply_upmap_items(raw, items, all_in) == [3, 1, 2]

    def test_next_step_waits_for_position_holding_its_target(self):
        # Target puts OSD 1 at position 0, but OSD 1 is still at position 1
        # (moving to 4): position 1 has to go first.
        raw = [0, 1, 2]
        items = coral.next_upmap_step(raw, [0, 1, 2], [1, 4, 2], all_in)
        assert coral.apply_upmap_items(raw, items, all_in) == [0, 4, 2]

    def test_align_replicas_keeps_staying_osds_in_their_slots(self):
        # OSD 271 stays (moving slots would cost a copy off and back on); 20 -> 951
        assert coral.align_replicas([20, 271], [271, 951]) == [951, 271]
        assert coral.align_replicas([1, 2, 3], [3, 4, 1]) == [1, 4, 3]

    def test_replicated_plan_target_is_aligned(self, logger):
        state = make_state({0: (1, 1), 1: (1, 1), 2: (1, 1)}, {"1.0": ([0, 1], [])})
        pg = state["pgs"]["1.0"]
        pg["original_up"], pg["original_upmaps"] = [0, 1], []
        pg["up"], pg["upmaps"] = [1, 2], [(0, 1), (1, 2)]    # planned: same members as [2, 1]
        plan = coral.build_upmap_plan(state)
        assert plan == {"1.0": [2, 1]}
        assert coral.next_upmap_step([0, 1], [0, 1], plan["1.0"], all_in) == [(0, 2)]

    # An EC 4+3 PG with failure domain cage: shard 2 goes from OSD 0 (cage-a1)
    # to OSD 10 (cage-b1) and shard 4 from OSD 4 (cage-b1) to OSD 5 (cage-a1).
    # Either alone puts two shards in one cage, and the mon then cancels every
    # upmap item of the PG.
    CAGE = {1: "cage-a2", 15: "cage-e2", 0: "cage-a1", 8: "cage-b2", 4: "cage-b1", 21: "cage-c1",
            2: "cage-d1", 10: "cage-b1", 5: "cage-a1", 9: "cage-c2", 22: "cage-f1"}

    def test_next_step_keeps_failure_domains_distinct(self):
        raw, current, target = [9, 15, 10, 8, 22, 21, 2], [1, 15, 0, 8, 4, 21, 2], [1, 15, 10, 8, 5, 21, 2]
        # without failure domains the step drops (10 -> 0) alone: two shards in cage-b1
        unsafe = coral.apply_upmap_items(raw, coral.next_upmap_step(raw, current, target, all_in), all_in)
        assert coral.failure_domain_conflicts(unsafe, self.CAGE.get) == 1
        # with them, both shards change together
        items = coral.next_upmap_step(raw, current, target, all_in, bucket_of=self.CAGE.get)
        assert sorted(items) == [(9, 1), (22, 5)]
        assert coral.apply_upmap_items(raw, items, all_in) == target

    def test_next_step_prefers_a_valid_single_shard_over_the_fullest(self):
        # Moving the fullest shard (OSD 0 -> 3) first would share bucket B with
        # OSD 1; moving OSD 1 -> 4 first is valid, so that goes first.
        buckets = {0: "A", 1: "B", 2: "C", 3: "B", 4: "D"}.get
        raw = [0, 1, 2]
        items = coral.next_upmap_step(raw, [0, 1, 2], [3, 4, 2], all_in, utilization={0: 0.9}, bucket_of=buckets)
        assert coral.apply_upmap_items(raw, items, all_in) == [0, 4, 2]

    def test_round_uses_pool_failure_domains(self, logger):
        cluster = MagicMock()
        cluster.mon_command.return_value = (0, b'', '')
        pgs = {"25.6f": stepping_pg([1, 15, 0, 8, 4, 21, 2], upmaps=[(9, 1), (10, 0), (22, 4)])}
        coral.apply_upmap_round(cluster, {"25.6f": [1, 15, 10, 8, 5, 21, 2]}, pgs, all_in, False, logger,
                                failure_domains={25: self.CAGE})
        assert json.loads(cluster.mon_command.call_args.args[0])["id"] == [9, 1, 22, 5]

    def test_next_step_can_restore_crush_placement(self):
        # Dropping an item is a single-shard step too
        raw = [0, 1]
        items = coral.next_upmap_step(raw, [5, 6], [0, 6], all_in)
        assert items == [(1, 6)]

    # Three shards trading racks in a cycle: shard 0 moves into rack B (which
    # shard 1 is leaving), shard 1 into C (shard 2) and shard 2 into A (shard
    # 0). No shard can move alone. Rack D is free.
    RACK = {0: "A", 1: "B", 2: "C", 3: "B", 4: "C", 5: "A", 6: "D", 7: "D"}
    CYCLE_TARGET = [3, 4, 5]

    def walk(self, raw, target, **kwargs):
        """Step from raw to target; return the up sets visited."""
        ups, items = [list(raw)], []
        while ups[-1] != target:
            items = coral.next_upmap_step(raw, ups[-1], target, all_in, bucket_of=self.RACK.get, **kwargs)
            ups.append(coral.apply_upmap_items(raw, items, all_in))
            assert len(ups) < 10
        return ups

    def test_cycle_hops_one_shard_through_a_free_domain(self):
        spares = {"A": [5], "B": [3], "C": [4], "D": [6, 7]}
        ups = self.walk([0, 1, 2], self.CYCLE_TARGET, spares=spares, spare_rank=lambda o: -o)
        # one shard at a time, four moves instead of three, every step valid
        for before, after in zip(ups, ups[1:]):
            assert sum(a != b for a, b in zip(before, after)) == 1
            assert coral.failure_domain_conflicts(after, self.RACK.get) == 0
        assert len(ups) == 5
        assert 7 in ups[1]      # spare_rank picks the temporary home

    def test_cycle_without_a_free_domain_moves_together(self):
        # without spares, or with spares only in racks the PG uses or wants
        for spares in (None, {"A": [5], "B": [3], "C": [4]}):
            ups = self.walk([0, 1, 2], self.CYCLE_TARGET, spares=spares, spare_rank=lambda o: o)
            assert ups == [[0, 1, 2], self.CYCLE_TARGET]

    def test_round_spreads_hops_and_avoids_nearfull_spares(self, logger):
        cluster = MagicMock()
        cluster.mon_command.return_value = (0, b'', '')
        rack = {**self.RACK, 8: "D"}
        pgs = {"1.0": stepping_pg([0, 1, 2]), "1.1": stepping_pg([0, 1, 2])}
        plan = {pgid: self.CYCLE_TARGET for pgid in pgs}
        coral.apply_upmap_round(cluster, plan, pgs, all_in, False, logger,
                                utilization={6: 0.9, 7: 0.1, 8: 0.2}, nearfull_ratio=0.85,
                                failure_domains={1: rack}, spare_osds={1: [6, 7, 8]})
        homes = [set(json.loads(c.args[0])["id"]) & {6, 7, 8} for c in cluster.mon_command.call_args_list]
        assert homes == [{7}, {8}]


def stepping_pg(up, acting=None, state="active+clean", upmaps=()):
    return {"up": list(up), "acting": list(acting if acting is not None else up),
            "state": state.split("+"), "upmaps": list(upmaps), "num_bytes": GiB}


class TestApplyUpmapRound:
    def test_one_shard_per_settled_pg(self, logger):
        cluster = MagicMock()
        cluster.mon_command.return_value = (0, b'', '')
        pgs = {
            "1.0": stepping_pg([0, 1, 2]),
            "1.1": stepping_pg([0, 1, 2], acting=[0, 1, 9], state="active+remapped+backfilling"),
            "1.2": stepping_pg([0, 1, 2], state="active+clean+remapped"),
            "1.3": stepping_pg([3, 4, 5], upmaps=[(0, 3), (1, 4), (2, 5)]),   # already there
        }
        plan = {pgid: [3, 4, 5] for pgid in pgs}
        started, remaining = coral.apply_upmap_round(cluster, plan, pgs, all_in, False, logger)
        assert (started, remaining) == (1, 3)
        cmd = json.loads(cluster.mon_command.call_args.args[0])
        assert cmd["pgid"] == "1.0" and cmd["id"] == [0, 3]

    def test_drops_dead_items_once_placement_is_right(self, logger):
        cluster = MagicMock()
        cluster.mon_command.return_value = (0, b'', '')
        # (7 -> 8) never applied (7 isn't in the PG); placement already matches
        pgs = {"1.0": stepping_pg([3, 1], upmaps=[(0, 3), (7, 8)])}
        started, remaining = coral.apply_upmap_round(cluster, {"1.0": [3, 1]}, pgs, all_in, False, logger)
        assert (started, remaining) == (1, 1)
        assert json.loads(cluster.mon_command.call_args.args[0])["id"] == [0, 3]


class TestFullnessPriority:
    def test_osd_info_records_raw_usage(self, logger):
        pg_dump = {"pg_map": {**PG_DUMP["pg_map"],
                              "osd_stats": [{"osd": 0, "kb": 100, "kb_used": 40}, {"osd": 1, "kb": 100}]}}
        result = coral.get_osd_info(make_mock_cluster({"pg dump": pg_dump}), logger)
        assert result[0]["used"] == 40 * 1024
        assert "used" not in result[1]    # not reported: projections fall back to planned data

    def test_nearfull_ratio_from_osd_dump_or_default(self, logger):
        assert coral.get_nearfull_ratio(make_mock_cluster({"osd dump": {**OSD_DUMP, "nearfull_ratio": 0.8}}), logger) == 0.8
        assert coral.get_nearfull_ratio(make_mock_cluster(), logger) == 0.85

    def test_osd_utilization(self):
        pg_dump = {"pg_map": {"osd_stats": [{"osd": 0, "kb": 200, "kb_used": 50}, {"osd": 1, "kb": 0}]}}
        assert coral.osd_utilization(pg_dump) == {0: 0.25}

    def test_balance_skips_recipient_that_would_pass_backfillfull(self, balance_cluster_state, logger):
        # OSD 2 is the only recipient, but another 1 GiB would take it past
        # 90% of 10 GiB. (OSD 0's target is 0, but the drain fallback can't use
        # OSD 2 either, and OSD 1 is already in the PG.)
        balance_cluster_state["osds"][2]["future_usage"] = int(8.5 * GiB)
        coral.balance_off_target_osds(balance_cluster_state, logger)
        assert balance_cluster_state["upmap_queue"] == {}

    def test_backfillfull_projection_uses_raw_usage(self, balance_cluster_state):
        osd = balance_cluster_state["osds"][2]
        assert coral.fits_below_backfillfull(balance_cluster_state, 2, "1.0")
        # 8.5 GiB already used on disk (overhead, omap, ...), nothing planned yet
        osd["used"] = int(8.5 * GiB)
        assert not coral.fits_below_backfillfull(balance_cluster_state, 2, "1.0")

    def test_rebuild_does_not_keep_upmap_onto_backfillfull_osd(self, logger):
        # Same as test_keeps_helpful_upmap_and_does_not_resend_it, but OSD 2
        # is nearly full on disk, so (0 -> 2) is dropped.
        state = make_state({0: (1, 2), 1: (1, 2), 2: (1, 2)},
                           {"1.0": ([2, 1], [(0, 2)]), "1.1": ([0, 1], [])})
        state["backfillfull_ratio"] = 0.9
        for osd in state["osds"].values():
            osd["used"] = GiB
        state["osds"][2]["used"] = int(9.5 * GiB)
        coral.rebuild_upmaps(state, logger)
        assert state["pgs"]["1.0"]["upmaps"] == []

    def test_rebuild_keeps_upmap_whose_removal_would_pass_backfillfull(self, logger):
        # Like test_drops_upmap_that_would_leave_source_under_floor: by PG
        # count (0 -> 2) isn't worth keeping, but sending PG 1.0 back would put
        # OSD 0 (8.5 GiB already used on disk) past 90% of 10 GiB.
        state = make_state({0: (2, 2), 1: (2, 2), 2: (0, 0)},
                           {"1.0": ([2, 1], [(0, 2)]), "1.1": ([0, 1], [])})
        state["backfillfull_ratio"] = 0.9
        for osd in state["osds"].values():
            osd["used"] = GiB
        state["osds"][0]["used"] = int(8.5 * GiB)
        coral.rebuild_upmaps(state, logger)
        assert state["pgs"]["1.0"]["upmaps"] == [(0, 2)]
        assert not coral.is_past_backfillfull(state, 0)

    def test_next_step_moves_fullest_shard_first(self):
        raw = [0, 1, 2]
        items = coral.next_upmap_step(raw, [0, 1, 2], [3, 4, 5], all_in, utilization={1: 0.5, 2: 0.9})
        assert coral.apply_upmap_items(raw, items, all_in) == [0, 1, 5]

    @staticmethod
    def _round(pgs, plan, utilization, nearfull_ratio=0.85):
        cluster = MagicMock()
        cluster.mon_command.return_value = (0, b'', '')
        result = coral.apply_upmap_round(cluster, plan, pgs, all_in, False, MagicMock(),
                                         utilization=utilization, nearfull_ratio=nearfull_ratio)
        sent = [json.loads(c.args[0])["pgid"] for c in cluster.mon_command.call_args_list]
        return result, sent

    def test_only_moves_off_nearfull_osds_start_while_any_remain(self):
        pgs = {"1.0": stepping_pg([0, 1]), "1.1": stepping_pg([5, 6]),
               "1.2": stepping_pg([3, 1], upmaps=[(0, 3), (7, 8)])}    # only drops a dead item
        plan = {"1.0": [2, 1], "1.1": [7, 6], "1.2": [3, 1]}
        (started, remaining), sent = self._round(pgs, plan, {0: 0.9})
        assert (started, remaining) == (2, 3)
        assert set(sent) == {"1.0", "1.2"}

    def test_nearfull_work_still_waiting_holds_others_back(self):
        # 1.0 still has to move off nearfull OSD 0 but is busy backfilling
        # something else, so it can't step this round — and 1.1 still waits.
        pgs = {"1.0": stepping_pg([0, 1], acting=[0, 9], state="active+remapped+backfilling"),
               "1.1": stepping_pg([5, 6])}
        plan = {"1.0": [2, 1], "1.1": [7, 6]}
        (started, remaining), sent = self._round(pgs, plan, {0: 0.9})
        assert (started, remaining) == (0, 2)

    def test_pg_leaving_nearfull_osd_steps_even_when_another_shard_must_go_first(self):
        # 1.0 must leave nearfull OSD 0, but 0's slot goes to OSD 1, which is
        # still in the PG: the first step has to move OSD 1's shard. That step
        # is still the nearfull PG's progress — it must start, not deadlock.
        pgs = {"1.0": stepping_pg([0, 1]), "1.1": stepping_pg([5, 6])}
        plan = {"1.0": [1, 2], "1.1": [7, 6]}
        (started, remaining), sent = self._round(pgs, plan, {0: 0.9})
        assert sent == ["1.0"]

    def test_stuck_nearfull_pg_does_not_hold_everything_back(self):
        # 1.0's move off nearfull OSD 0 is stuck (its target is backfillfull);
        # waiting on it would stall every other PG indefinitely.
        pgs = {"1.0": stepping_pg([0, 1], acting=[0, 9], state="active+remapped+backfill_wait+backfill_toofull"),
               "1.1": stepping_pg([5, 6])}
        plan = {"1.0": [2, 1], "1.1": [7, 6]}
        (started, remaining), sent = self._round(pgs, plan, {0: 0.9})
        assert sent == ["1.1"]

    def test_nearfull_osd_outside_the_plan_holds_nothing_back(self):
        pgs = {"1.0": stepping_pg([0, 1]), "1.1": stepping_pg([5, 6])}
        plan = {"1.0": [2, 1], "1.1": [7, 6]}
        (started, remaining), sent = self._round(pgs, plan, {9: 0.95})
        assert (started, remaining) == (2, 2)

    def test_fullest_source_goes_first(self):
        pgs = {"1.0": stepping_pg([0, 1]), "1.1": stepping_pg([5, 6])}
        plan = {"1.0": [2, 1], "1.1": [7, 6]}
        (started, _), sent = self._round(pgs, plan, {0: 0.88, 5: 0.95})
        assert sent == ["1.1", "1.0"]


class FakeSteppingCluster:
    """Answers osd dump / pg dump and applies pg-upmap-items. A PG whose items
    change backfills for one round, then settles."""

    def __init__(self, raw, upmaps=None, size=3):
        self.raw = raw
        self.upmaps = {pgid: list(upmaps.get(pgid, [])) for pgid in raw} if upmaps else {pgid: [] for pgid in raw}
        self.backfilling = set()
        self.weight = 1
        self.commands = []

    def up(self, pgid):
        return coral.apply_upmap_items(self.raw[pgid], self.upmaps[pgid], all_in)

    def mon_command(self, cmd_json, inbuf, timeout=5):
        cmd = json.loads(cmd_json)
        prefix = cmd["prefix"]
        if prefix == "osd dump":
            return 0, json.dumps({
                "crush_version": 1,
                "osds": [{"osd": o, "up": 1, "in": 1, "weight": self.weight} for o in range(12)],
                "pools": [{"pool": 1, "pg_num": len(self.raw), "size": 3, "crush_rule": 0}],
                "pg_upmap_items": [{"pgid": pgid, "mappings": [{"from": f, "to": t} for f, t in items]}
                                   for pgid, items in self.upmaps.items() if items],
            }).encode(), ""
        if prefix == "pg dump":
            stats = []
            for pgid in self.raw:
                busy = pgid in self.backfilling
                stats.append({"pgid": pgid, "up": self.up(pgid),
                              "acting": [] if busy else self.up(pgid),
                              "state": "active+remapped+backfilling" if busy else "active+clean",
                              "stat_sum": {"num_bytes": 1}})
            self.backfilling = set()   # everything finishes by the next round
            return 0, json.dumps({"pg_map": {"pg_stats": stats}}).encode(), ""
        if prefix in ("osd pg-upmap-items", "osd rm-pg-upmap-items"):
            ids = cmd.get("id", [])
            before = self.up(cmd["pgid"])
            self.upmaps[cmd["pgid"]] = list(zip(ids[::2], ids[1::2]))
            self.commands.append((cmd["pgid"], before, self.up(cmd["pgid"])))
            self.backfilling.add(cmd["pgid"])
            return 0, b"", ""
        raise AssertionError(prefix)


class TestRunUpmapPlan:
    def test_moves_one_shard_per_pg_per_round_until_done(self, logger):
        fake = FakeSteppingCluster({"1.0": [0, 1, 2], "1.1": [3, 4, 5]},
                                   upmaps={"1.1": [(3, 9)]})
        plan = {"1.0": [6, 7, 8], "1.1": [3, 10, 5]}
        fingerprint = coral.plan_fingerprint(json.loads(fake.mon_command('{"prefix": "osd dump"}', b"")[1]))
        sleeps = []
        result = coral.run_upmap_plan(fake, plan, fingerprint, 60, logger, sleep=sleeps.append)

        assert result == "done"
        assert fake.up("1.0") == [6, 7, 8] and fake.up("1.1") == [3, 10, 5]
        # every command moved exactly one shard
        for pgid, before, after in fake.commands:
            assert sum(a != b for a, b in zip(before, after)) == 1, (pgid, before, after)
        # 1.0 needs 3 single-shard steps; 1.1 needs 2 (drop 3 -> 9, then 4 -> 10).
        # A PG backfills for a round after each step, so 1.0 takes rounds 1, 3, 5.
        assert sum(1 for c in fake.commands if c[0] == "1.0") == 3
        assert sum(1 for c in fake.commands if c[0] == "1.1") == 2
        assert sleeps and set(sleeps) == {60}

    def test_cycles_step_one_shard_at_a_time(self, logger):
        fake = FakeSteppingCluster({"1.0": [0, 1, 2]})
        fingerprint = coral.plan_fingerprint(json.loads(fake.mon_command('{"prefix": "osd dump"}', b"")[1]))
        rack = {0: "A", 1: "B", 2: "C", 3: "B", 4: "C", 5: "A", 6: "D"}
        result = coral.run_upmap_plan(fake, {"1.0": [3, 4, 5]}, fingerprint, 60, logger, sleep=lambda s: None,
                                      failure_domains={1: rack}, pool_osds={1: list(rack)})
        assert result == "done" and fake.up("1.0") == [3, 4, 5]
        assert [sum(a != b for a, b in zip(before, after)) for _, before, after in fake.commands] == [1, 1, 1, 1]

    def test_replans_when_the_cluster_changes(self, logger):
        fake = FakeSteppingCluster({"1.0": [0, 1, 2]})
        fingerprint = coral.plan_fingerprint(json.loads(fake.mon_command('{"prefix": "osd dump"}', b"")[1]))
        fake.weight = 0.5   # someone reweighted an OSD
        assert coral.run_upmap_plan(fake, {"1.0": [6, 7, 8]}, fingerprint, 60, logger, sleep=lambda s: None) == "replan"
        assert fake.commands == []


class TestPrintUpmapPlan:
    def test_prints_first_round_and_summary(self, logger, capsys):
        state = make_state({0: (0, 0), 1: (1, 1), 2: (0, 1), 3: (1, 1)},
                           {"1.0": ([0, 1], [])})
        state["pgs"]["1.0"]["state"] = ["active", "clean"]
        state["pgs"]["1.0"]["original_up"] = [0, 1]
        state["pgs"]["1.0"]["original_upmaps"] = []
        coral.print_upmap_plan(state, {"1.0": [3, 2]}, 60, logger)
        out = capsys.readouterr().out
        assert "ceph osd pg-upmap-items 1.0 0 3" in out
        assert "1 PG(s) to change (2 shard move(s) in total)" in out


class TestDisplayUsage:
    @staticmethod
    def _strip_ansi(s):
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", s)

    def _state(self, osds):
        # Helper that fills in just enough fields for display_usage
        return {"osds": {
            i: {
                "device_class": cfg.get("device_class", "hdd"),
                "crush_weight": cfg.get("crush_weight", 1.0),
                "size": cfg.get("size", 10 * GiB),
                "current_usage": cfg.get("current_usage", 0),
                "current_pgs": cfg.get("current_pgs", 0),
                "future_usage": cfg.get("future_usage", 0),
                "future_pgs": cfg.get("future_pgs", 0),
            }
            for i, cfg in osds.items()
        }}

    def test_empty_state_prints_nothing(self, capsys):
        coral.display_usage({"osds": {}})
        assert capsys.readouterr().out == ""

    def test_borders_align_across_rows(self, capsys):
        state = self._state({
            0:   {"current_pgs": 1, "future_pgs": 2},
            10:  {"current_pgs": 50, "future_pgs": 60},
            127: {"current_pgs": 1234, "future_pgs": 1240},
        })
        coral.display_usage(state)
        lines = [self._strip_ansi(l) for l in capsys.readouterr().out.splitlines()]
        # Every line should be the same visible length (table is rectangular)
        widths = {len(l) for l in lines}
        assert len(widths) == 1, f"line widths differ: {widths}\n" + "\n".join(lines)

    def test_zero_size_osd_does_not_divide_by_zero(self, capsys):
        state = self._state({0: {"size": 0, "current_usage": 0, "future_usage": 0}})
        coral.display_usage(state)  # must not raise
        assert "0.0%" in self._strip_ansi(capsys.readouterr().out)

    def test_includes_expected_column_headers(self, capsys):
        state = self._state({0: {}})
        coral.display_usage(state)
        out = self._strip_ansi(capsys.readouterr().out)
        for header in ("OSD", "Class", "Weight", "Size",
                       "Current Usage", "Future Usage", "Change"):
            assert header in out


class TestDisplayPgsByPool:
    @staticmethod
    def _strip_ansi(s):
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", s)

    def test_prints_label_per_osd(self, base_cluster_state, capsys):
        coral.display_pgs_by_pool(base_cluster_state)
        out = self._strip_ansi(capsys.readouterr().out)
        assert "osd.0" in out
        assert "osd.1" in out

    def test_marks_over_target_as_over(self, base_cluster_state, capsys):
        # OSD 1 default: future=1, ceil=1. Bump count above ceil → OVER.
        base_cluster_state["osds"][1]["future_pgs_by_pool"][1] = 5
        coral.display_pgs_by_pool(base_cluster_state)
        out = self._strip_ansi(capsys.readouterr().out)
        assert "OVER" in out

    def test_marks_under_target_as_under(self, base_cluster_state, capsys):
        # Raise OSD 0's floor so its count (0) lands under it
        base_cluster_state["osds"][0]["target_pgs_by_pool"][1] = (2, 3)
        coral.display_pgs_by_pool(base_cluster_state)
        out = self._strip_ansi(capsys.readouterr().out)
        assert "UNDER" in out

    def test_marks_on_target_as_ok(self, base_cluster_state, capsys):
        coral.display_pgs_by_pool(base_cluster_state)
        out = self._strip_ansi(capsys.readouterr().out)
        assert "OK" in out
        assert "OVER" not in out
        assert "UNDER" not in out

    def test_shows_floor_ceil_and_future_count(self, base_cluster_state, capsys):
        coral.display_pgs_by_pool(base_cluster_state)
        out = self._strip_ansi(capsys.readouterr().out)
        assert "Future PGs" in out
        assert "Floor" in out
        assert "Ceil" in out
        assert "rbd" in out

    def test_skips_osd_with_no_pool_entries(self, capsys):
        state = {
            "osds": {
                0: {"future_pgs_by_pool": {}, "target_pgs_by_pool": {}},
            },
            "pools": {},
        }
        coral.display_pgs_by_pool(state)
        out = self._strip_ansi(capsys.readouterr().out)
        assert "osd.0" not in out

    def test_table_lines_have_uniform_width(self, capsys):
        # Mix of short and long pool names exercises dynamic width sizing.
        long_name = "default.rgw.buckets.index"
        state = {
            "osds": {
                0: {
                    "future_pgs_by_pool": {1: 0, 2: 0},
                    "target_pgs_by_pool": {1: (0, 1), 2: (0, 1)},
                },
                127: {
                    "future_pgs_by_pool": {1: 0, 2: 0},
                    "target_pgs_by_pool": {1: (0, 1), 2: (0, 1)},
                },
            },
            "pools": {1: {"name": "rbd"}, 2: {"name": long_name}},
        }
        coral.display_pgs_by_pool(state)
        out = self._strip_ansi(capsys.readouterr().out)
        # Every line that's part of a table (contains any box-drawing char)
        # should be the same visible length.
        box_chars = set("┌┐└┘├┤┬┴┼│─")
        table_lines = [l for l in out.splitlines() if any(c in l for c in box_chars)]
        widths = {len(l) for l in table_lines}
        assert len(widths) == 1, f"table line widths differ: {widths}\n" + "\n".join(table_lines)
