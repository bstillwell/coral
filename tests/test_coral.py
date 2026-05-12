from unittest.mock import MagicMock
import pytest

import coral
from conftest import make_mock_cluster, GiB, OSD_DUMP, POOL_LS_EC


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
        cluster = make_mock_cluster({"osd pool ls": POOL_LS_EC})
        result = coral.get_pool_info(cluster, logger)
        assert result[1]["type"] == "erasure"
        assert result[1]["k"] == 4
        assert result[1]["m"] == 2


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


class TestCalculatePgDistribution:
    @staticmethod
    def _state(osd_weights, pools, valid_osds):
        return {
            "osds": {osd_id: {"crush_weight": w} for osd_id, w in osd_weights.items()},
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

    def test_invoked_by_get_cluster_state(self, mock_cluster, logger):
        state = coral.get_cluster_state(mock_cluster, logger)
        # Fixture: 2 equal-weight OSDs, pool 1 with 128 PGs, size 3
        assert state["osds"][0]["target_pgs_by_pool"][1] == (192, 192)


class TestCalculateUsage:
    def test_acting_set_populates_current_usage(self, base_cluster_state, logger):
        coral.calculate_usage(base_cluster_state, logger)
        assert base_cluster_state["osds"][0]["current_usage"] == GiB
        assert base_cluster_state["osds"][0]["current_pgs"] == 1

    def test_up_set_populates_future_usage(self, base_cluster_state, logger):
        coral.calculate_usage(base_cluster_state, logger)
        assert base_cluster_state["osds"][1]["future_usage"] == GiB
        assert base_cluster_state["osds"][1]["future_pgs"] == 1

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
        base_cluster_state["osds"][1]["future_usage"] = GiB
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert (0, 1) not in base_cluster_state["pgs"]["1.0"]["upmaps"]

    def test_adds_bytes_back_to_from_osd(self, base_cluster_state, logger):
        base_cluster_state["osds"][1]["future_usage"] = GiB
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert base_cluster_state["osds"][0]["future_usage"] == GiB

    def test_subtracts_bytes_from_to_osd(self, base_cluster_state, logger):
        base_cluster_state["osds"][1]["future_usage"] = GiB
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert base_cluster_state["osds"][1]["future_usage"] == 0

    def test_enqueues_pgid(self, base_cluster_state, logger):
        coral.queue_upmap_mapping_removal(base_cluster_state, "1.0", (0, 1), logger)
        assert "1.0" in base_cluster_state["upmap_queue"]


class TestRemoveOverfullMappings:
    def test_enqueues_removal_when_osd_overfull(self, base_cluster_state, logger):
        base_cluster_state["osds"][1]["future_usage"] = int(9.5 * GiB)  # 95% > 90%
        coral.remove_overfull_mappings(base_cluster_state, logger)
        assert "1.0" in base_cluster_state["upmap_queue"]

    def test_leaves_queue_empty_when_osd_underfull(self, base_cluster_state, logger):
        base_cluster_state["osds"][1]["future_usage"] = int(8 * GiB)  # 80% < 90%
        coral.remove_overfull_mappings(base_cluster_state, logger)
        assert "1.0" not in base_cluster_state["upmap_queue"]

    def test_recalculates_usage_after_removal(self, base_cluster_state, logger):
        base_cluster_state["osds"][1]["future_usage"] = int(9.5 * GiB)
        coral.remove_overfull_mappings(base_cluster_state, logger)
        assert base_cluster_state["osds"][0]["future_usage"] == GiB
