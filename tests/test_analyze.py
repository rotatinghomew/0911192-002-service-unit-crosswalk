"""区划调整后公平比较与保底核查的本地测试。"""
import contextlib
import io
from pathlib import Path
import unittest

import analyze
from inspect_inputs import load_tables


DATA = Path(__file__).resolve().parents[1] / "data"


def setup_module_state(received_before=None):
    tables = load_tables(DATA)
    policy = analyze.load_policy(DATA / "policy.json")
    mapping = analyze.mapping_at(tables["locations"], analyze.quarter_end(policy["current"]))
    buckets, _ = analyze.build_buckets(
        tables, mapping, policy["commitments"], policy["history"],
        received_before=received_before)
    hists = analyze.historical_by_district(buckets)
    curs, _ = analyze.current_by_district(tables, mapping, policy["commitments"], policy["current"])
    return tables, policy, mapping, buckets, hists, curs


class CaliberTests(unittest.TestCase):
    def test_band_status_boundaries(self):
        # 旧档左开右闭；严格超过承诺才算超时；空上界为开放尾部
        self.assertEqual(analyze.band_status(0, 15, 30), "none")
        self.assertEqual(analyze.band_status(0, 30, 30), "none")    # 上界等于承诺：不超时
        self.assertEqual(analyze.band_status(30, None, 30), "all")  # 下界等于承诺：整档超时
        self.assertEqual(analyze.band_status(45, None, 30), "all")
        self.assertEqual(analyze.band_status(15, 45, 30), "straddle")
        self.assertEqual(analyze.band_status(10, 30, 20), "straddle")
        self.assertEqual(analyze.band_status(None, 20, 20), "none")
        self.assertEqual(analyze.band_status(None, None, 20), "straddle")

    def test_mapping_respects_effective_date(self):
        tables = load_tables(DATA)
        self.assertEqual(analyze.mapping_at(tables["locations"], "2025-12-31"), {})
        mapping = analyze.mapping_at(tables["locations"], "2026-09-30")
        self.assertEqual(len(mapping), 6)
        self.assertEqual(mapping["h1"], ("L1", "A"))
        self.assertEqual(mapping["h2"], ("L1", "B"))


class BucketTests(unittest.TestCase):
    def setUp(self):
        _t, _p, _m, buckets, _h, _c = setup_module_state()
        self.buckets = {b.bucket: b for b in buckets}

    def test_located_deducted_not_double_counted(self):
        # 街巷级总数已含定位抽核记录：先扣除，余数只留归属限制
        self.assertEqual(self.buckets["x2"].remainder, 22 - 11 - 6)
        self.assertEqual(self.buckets["x5"].remainder, 12 - 4 - 7)
        self.assertEqual(self.buckets["x2"].located, {"A": 11, "B": 6})

    def test_remainder_candidates_follow_lane(self):
        self.assertEqual(self.buckets["x2"].candidates, ("A", "B"))
        self.assertEqual(self.buckets["x5"].candidates, ("B", "C"))
        self.assertEqual(self.buckets["x6"].candidates, ("A",))
        self.assertEqual(self.buckets["x7"].candidates, ("C",))

    def test_no_population_proportion_split(self):
        # 余数不按全体人口比例分派：只能整批在候选片区间移动
        x2 = self.buckets["x2"]
        self.assertEqual(x2.remainder, 5)
        self.assertNotIn("share", x2.__dict__ if hasattr(x2, "__dict__") else {})


class HistoryTests(unittest.TestCase):
    def setUp(self):
        _t, _p, _m, _b, hists, _c = setup_module_state()
        self.hists = hists

    def test_single_candidate_lane_forces_attribution(self):
        a_cert = self.hists[("A", "certificate")]
        self.assertEqual((a_cert.min_total, a_cert.max_total), (7, 7))
        c_cert = self.hists[("C", "certificate")]
        self.assertEqual((c_cert.min_timeout, c_cert.max_timeout), (4, 4))
        self.assertEqual(c_cert.rate_lo, 1.0)

    def test_ranges_not_midpoints(self):
        # 历史只有区间：保留端点，不按区间中心补造
        a_cert = self.hists[("A", "certificate")]
        self.assertEqual((a_cert.min_timeout, a_cert.max_timeout), (0, 7))
        a_social = self.hists[("A", "social")]
        self.assertEqual((a_social.min_total, a_social.max_total), (11, 35))
        self.assertEqual((a_social.min_timeout, a_social.max_timeout), (0, 20))

    def test_district_ranges(self):
        b_social = self.hists[("B", "social")]
        self.assertEqual((b_social.min_total, b_social.max_total), (15, 74))
        self.assertEqual((b_social.min_timeout, b_social.max_timeout), (9, 25))
        c_social = self.hists[("C", "social")]
        self.assertEqual((c_social.min_total, c_social.max_total), (4, 39))
        self.assertEqual((c_social.min_timeout, c_social.max_timeout), (4, 5))
        self.assertAlmostEqual(c_social.rate_lo, 4 / 39)

    def test_services_not_merged(self):
        # 业务类型只在可比承诺内比较，不跨口径合并
        self.assertIn(("A", "social"), self.hists)
        self.assertIn(("A", "certificate"), self.hists)
        self.assertNotIn(("A", "social+certificate"), self.hists)
        self.assertNotIn(("B", "certificate"), self.hists)


class CurrentTests(unittest.TestCase):
    def setUp(self):
        _t, _p, _m, _b, _h, curs = setup_module_state()
        self.curs = curs

    def test_interrupted_registered_separately(self):
        c_social = self.curs[("C", "social")]
        self.assertEqual((c_social.done, c_social.timeout, c_social.interrupted), (1, 0, 1))

    def test_timeout_is_strictly_over_commitment(self):
        b_social = self.curs[("B", "social")]
        self.assertEqual(b_social.done, 3)
        self.assertEqual(b_social.timeout, 1)  # 30 分钟不算超时，41 分钟算


class VerdictTests(unittest.TestCase):
    def setUp(self):
        _t, _p, _m, _b, hists, curs = setup_module_state()
        self.verdicts = {key: analyze.compare(hists.get(key), curs.get(key))
                         for key in set(hists) | set(curs)}

    def test_genuine_improvement(self):
        self.assertEqual(self.verdicts[("C", "social")].label, "确实改善")

    def test_direction_undetermined_by_attribution(self):
        verdict = self.verdicts[("A", "social")]
        self.assertEqual(verdict.label, "方向未定")
        self.assertTrue(any("归属未定" in r for r in verdict.reasons))
        self.assertTrue(any("口径未定" in r for r in verdict.reasons))

    def test_direction_undetermined_by_caliber_only(self):
        # L3 只通向 A，归属确定；但档跨承诺，方向仍未定
        verdict = self.verdicts[("A", "certificate")]
        self.assertEqual(verdict.label, "方向未定")
        self.assertFalse(verdict.hist.flex_attribution)
        self.assertTrue(any("口径未定" in r for r in verdict.reasons))

    def test_not_improved_when_level_with_history(self):
        self.assertEqual(self.verdicts[("C", "certificate")].label, "未见改善")

    def test_rollup_partial_support(self):
        labels = {"social": self.verdicts[("C", "social")],
                  "certificate": self.verdicts[("C", "certificate")]}
        self.assertEqual(analyze.rollup(labels), "只有一部分比较可支持")


class JointConstraintTests(unittest.TestCase):
    def test_same_remainder_cannot_prove_both_districts(self):
        _t, _p, _m, buckets, _h, _c = setup_module_state()
        constraints = {(item["bucket"].bucket, item["pair"]): item
                       for item in analyze.joint_constraints(buckets, "social")}
        x3 = constraints[("x3", ("A", "B"))]
        self.assertEqual(x3["max"], (4, 6))
        # 单方上界不能同时成立：共同归属下较小一方至多 3 件
        self.assertEqual(x3["joint_min_max"], 3)
        self.assertLess(x3["joint_min_max"], min(x3["max"]))
        x5 = constraints[("x5", ("B", "C"))]
        self.assertEqual(x5["max"], (8, 5))


class FloorTests(unittest.TestCase):
    def setUp(self):
        self.tables, self.policy, _m, _b, hists, curs = setup_module_state()
        self.aid = analyze.aid_districts(self.tables["locations"], ["west"])
        self.needs = analyze.floor_needs(self.aid, list(self.policy["commitments"]), hists, curs)

    def test_aid_scope_follows_old_district(self):
        self.assertEqual(self.aid, ["A", "B"])

    def test_needs_cover_union_not_cherry_picked(self):
        # A、B 的 social 都需要保底，即使 x3 余数不能同时全归两方
        self.assertIn(("A", "social"), self.needs)
        self.assertIn(("B", "social"), self.needs)
        self.assertIn(("A", "certificate"), self.needs)
        self.assertNotIn(("B", "certificate"), self.needs)  # 无任何超时证据

    def test_announced_shifts_never_move(self):
        announced, groups = analyze.plan_choices(self.tables["windows"])
        self.assertEqual(announced, ["w1", "w5"])
        self.assertEqual(groups["staff2"], ["w2", "w3"])

    def test_valid_plan_keeps_floor(self):
        announced, groups, valid = analyze.evaluate_plans(self.tables["windows"], self.needs)
        minimal = min(len(plan) for plan in valid)
        self.assertIn(["w1", "w5", "w2", "w4", "w7"], valid)
        self.assertEqual(minimal, 5)
        # 迁往东门（w3）会让 B 失去第二个可到达时段
        self.assertFalse(any("w3" in plan for plan in valid))

    def test_same_group_is_one_resource(self):
        with self.assertRaisesRegex(ValueError, "同一值班资源"):
            analyze.parse_plan("w2,w3", self.tables["windows"])
        with self.assertRaisesRegex(ValueError, "承诺班次"):
            analyze.parse_plan("w1,w4", self.tables["windows"])

    def test_check_plan_reports_shortfall(self):
        by_id = {w["shift"]: w for w in self.tables["windows"]}
        shortfalls = analyze.check_plan(by_id, ["w1", "w5", "w3", "w4", "w7"], self.needs)
        self.assertIn(("B", "social", 1), shortfalls)


class PublicationTests(unittest.TestCase):
    def test_new_attributions_do_not_rewrite_published_view(self):
        _t, _p, _m, _b, hists_now, _c = setup_module_state()
        _t2, _p2, _m2, _b2, hists_pub, _c2 = setup_module_state(received_before="2026-10-01")
        # 已公开口径（冻结）：z4、z5 尚未收到
        self.assertEqual(hists_pub[("B", "social")].min_timeout, 2)
        # 当前分析：新确认归属重新计入
        self.assertEqual(hists_now[("B", "social")].min_timeout, 9)


class EvidenceTests(unittest.TestCase):
    def test_gaps_point_to_lane_and_band(self):
        _t, _p, _m, buckets, _h, _c = setup_module_state()
        social_gaps = analyze.evidence_gaps(buckets, "social")
        self.assertTrue(any("L1" in g and "x2" in g and "定位" in g for g in social_gaps))
        self.assertTrue(any("L1" in g and "x3" in g for g in social_gaps))
        self.assertTrue(any("x2" in g and "明细时长" in g for g in social_gaps))
        cert_gaps = analyze.evidence_gaps(buckets, "certificate")
        self.assertTrue(any("L3" in g and "x6" in g and "明细时长" in g for g in cert_gaps))
        # L3 只通向 A：归属确定，不应要求补定位
        self.assertFalse(any("x6" in g and "定位" in g for g in cert_gaps))


class ReportTests(unittest.TestCase):
    def run_main(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = analyze.main(list(argv))
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_full_report(self):
        text = self.run_main("--published-before", "2026-10-01", "--plan", "w2,w4,w7")
        for marker in ["确实改善", "只有一部分比较可支持", "口径或归属使方向未定",
                       "满足保底", "失去保底", "需补证", "已公开口径", "不改写"]:
            self.assertIn(marker, text)

    def test_bad_plan_is_reported(self):
        text = self.run_main("--plan", "w2,w3")
        self.assertIn("指定排法无效", text)


if __name__ == "__main__":
    unittest.main()
