"""区划调整后的等待公平比较与周末窗口保底核查。

口径要点（与交接说明一致）：
- 能定位到楼栋的记录先从街巷档总数中确切扣除，余数只施加归属限制，
  不按今天的全体人口比例分派历史办理事件；
- 同一批未定位人数不能同时记给两个新片区（联合归属约束），也不提供
  把各片区各自最有利数字拼接而成的全局结论；
- 历史等候档只有区间时保留区间，不按区间中心补造明细；
- 业务类型只在可比服务承诺内比较，不跨口径合并；预约变更造成的中断
  另有登记，不计入已完成时长；
- 已对外通知的班次保留承诺、不移动；新确认的楼栋归属只影响当前分析
  与尚未通知的迁移决定，不改写已公开口径。
"""
from __future__ import annotations

import argparse
import datetime
import json
from dataclasses import dataclass, field
from itertools import combinations, product
from pathlib import Path

from inspect_inputs import load_tables

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 基础口径

def parse_minutes(text):
    """空值表示未给出（开放端），不等于零。"""
    text = (text or "").strip()
    return int(text) if text else None


def band_status(lower, upper, commitment):
    """旧档左开右闭 (lower, upper]，空上界为开放尾部；严格超过承诺算超时。

    返回 "all"（整档必超时）、"none"（整档不超时）或 "straddle"
    （档跨承诺，档内超时人数只能给区间，不补造明细）。
    """
    if lower is not None and lower >= commitment:
        return "all"
    if upper is not None and upper <= commitment:
        return "none"
    return "straddle"


def quarter_end(quarter):
    year_text, q_text = quarter.split("-Q")
    year, month = int(year_text), int(q_text) * 3
    if month == 12:
        return f"{year}-12-31"
    return (datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)).isoformat()


def load_policy(path):
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "history": policy["比较期间"][0],
        "current": policy["比较期间"][1],
        "commitments": {name: int(minutes) for name, minutes in policy["可比承诺分钟"].items()},
        "raw": policy,
    }


def mapping_at(locations, when):
    """截至 when（含）最新生效的楼栋 → (街巷, 现行片区) 对照。"""
    chosen = {}
    for row in locations:
        if row["effective"] <= when:
            prev = chosen.get(row["building"])
            if prev is None or prev[0] <= row["effective"]:
                chosen[row["building"]] = (row["effective"], row["lane"], row["district_to"])
    return {building: (lane, district) for building, (eff, lane, district) in chosen.items()}


def aid_districts(locations, aid_from):
    """原帮扶片区的现行承接片区。"""
    return sorted({row["district_to"] for row in locations if row["district_from"] in set(aid_from)})


# ---------------------------------------------------------------- 历史街巷档

@dataclass
class Bucket:
    bucket: str
    service: str
    lane: str
    lower: object
    upper: object
    status: str
    count: int
    located: dict      # 现行片区 → 已确切归属人数（先从总数扣除）
    remainder: int     # 街巷级余数（只施加归属限制，不按比例分派）
    candidates: tuple  # 余数可能归属的现行片区

    def band_text(self):
        upper = "∞" if self.upper is None else str(self.upper)
        return f"({self.lower}, {upper}]"


def build_buckets(tables, mapping, commitments, history_quarter, received_before=None):
    """整理历史季度的街巷档。定位记录先确切扣除；余数只留归属限制。

    received_before：只采用该日期之前收到的定位记录，用于复现已公开口径。
    返回 (buckets, warnings)。
    """
    lane_candidates = {}
    for _building, (lane, district) in mapping.items():
        lane_candidates.setdefault(lane, set()).add(district)

    located_by_bucket = {}
    for row in tables["located_counts"]:
        if received_before is not None and row["received"] >= received_before:
            continue
        building = row["building"]
        if building not in mapping:
            raise ValueError(f"定位记录 {row['record']}: 楼栋 {building} 没有生效中的归属对照")
        lane, district = mapping[building]
        located_by_bucket.setdefault(row["bucket"], []).append(
            (row["record"], lane, district, int(row["count"])))

    buckets, warnings = [], []
    for row in tables["legacy_bins"]:
        if row["quarter"] != history_quarter:
            continue
        service = row["service"]
        if service not in commitments:
            warnings.append(f"档 {row['bucket']}: 服务 {service} 无可比承诺，排除在共同口径之外")
            continue
        lower, upper = parse_minutes(row["lower"]), parse_minutes(row["upper"])
        count = int(row["count"])
        located = {}
        for record, lane, district, n in located_by_bucket.get(row["bucket"], []):
            if lane != row["lane"]:
                raise ValueError(
                    f"定位记录 {record}: 楼栋所在街巷 {lane} 与档 {row['bucket']} 的街巷 {row['lane']} 不符")
            located[district] = located.get(district, 0) + n
        remainder = count - sum(located.values())
        if remainder < 0:
            raise ValueError(f"档 {row['bucket']}: 定位人数超过街巷档总数，请核查交接资料")
        candidates = tuple(sorted(lane_candidates.get(row["lane"], ())))
        if remainder > 0 and not candidates:
            warnings.append(f"档 {row['bucket']}: 街巷 {row['lane']} 无楼栋对照，余 {remainder} 人无法归属")
        buckets.append(Bucket(
            bucket=row["bucket"], service=service, lane=row["lane"],
            lower=lower, upper=upper,
            status=band_status(lower, upper, commitments[service]),
            count=count, located=located, remainder=remainder,
            candidates=candidates,
        ))
    return buckets, warnings


@dataclass
class Hist:
    district: str
    service: str
    min_total: int = 0
    max_total: int = 0
    min_timeout: int = 0
    max_timeout: int = 0
    hi_total: int = 0            # 取超时率上界情形下的总人数
    flex_attribution: bool = False  # 存在跨区街巷的未定位余数
    straddle: bool = False          # 存在跨承诺的区间档
    bands: list = field(default_factory=list)  # (bucket, 确切人数, 浮动人数)

    @property
    def rate_lo(self):
        return self.min_timeout / self.max_total if self.max_total else None

    @property
    def rate_hi(self):
        if self.hi_total:
            return self.max_timeout / self.hi_total
        return 0.0 if self.max_timeout == 0 else None


def historical_by_district(buckets):
    """历史街巷档按现行片区的可取范围；余数不按比例分派，只施加限制。"""
    hists = {}
    for b in buckets:
        for d in sorted(set(b.candidates) | set(b.located)):
            located = b.located.get(d, 0)
            forced = b.remainder if b.candidates == (d,) else 0
            flex = b.remainder if len(b.candidates) > 1 and d in b.candidates else 0
            if located == 0 and forced == 0 and flex == 0:
                continue
            h = hists.setdefault((d, b.service), Hist(d, b.service))
            h.min_total += located + forced
            h.max_total += located + forced + flex
            h.hi_total += located + forced
            if b.status == "all":
                h.min_timeout += located + forced
                h.max_timeout += located + forced + flex
                h.hi_total += flex
            elif b.status == "straddle":
                h.max_timeout += located + forced + flex
                h.hi_total += flex
                h.straddle = True
            if flex:
                h.flex_attribution = True
            h.bands.append((b, located + forced, flex))
    return hists


# ---------------------------------------------------------------- 现行明细

@dataclass
class Cur:
    district: str
    service: str
    done: int = 0
    timeout: int = 0
    interrupted: int = 0
    minutes: list = field(default_factory=list)

    @property
    def rate(self):
        return self.timeout / self.done if self.done else None


def current_by_district(tables, mapping, commitments, current_quarter):
    """现行季度按楼栋归片区的明细统计；中断另登记，不计入已完成时长。"""
    curs, warnings = {}, []
    for row in tables["service_events"]:
        if row["quarter"] != current_quarter:
            continue
        service = row["service"]
        if service not in commitments:
            warnings.append(f"办件 {row['case']}: 服务 {service} 无可比承诺，排除在共同口径之外")
            continue
        building = row["building"]
        if building not in mapping:
            raise ValueError(f"办件 {row['case']}: 楼栋 {building} 没有生效中的归属对照")
        _lane, district = mapping[building]
        c = curs.setdefault((district, service), Cur(district, service))
        if row["state"] == "done":
            c.done += 1
            minutes = int(row["minutes"])
            c.minutes.append(minutes)
            if minutes > commitments[service]:
                c.timeout += 1
        elif row["state"] == "interrupted":
            c.interrupted += 1
        else:
            raise ValueError(f"办件 {row['case']}: 未知状态 {row['state']}")
    return curs, warnings


# ---------------------------------------------------------------- 比较结论

@dataclass
class Verdict:
    label: str
    reasons: list
    hist: object
    cur: object


def compare(hist, cur):
    """同一（片区, 服务）在历史可取范围与现行明细之间的方向判定。"""
    if hist is None or hist.max_total == 0:
        return Verdict("无共同口径", ["本片区该服务没有可归属的历史记录"], hist, cur)
    if cur is None or cur.done == 0:
        return Verdict("方向未定", ["现行没有已完成的明细记录"], hist, cur)
    rate, lo, hi = cur.rate, hist.rate_lo, hist.rate_hi
    if lo is not None and rate < lo:
        return Verdict("确实改善", [f"现行超时率 {pct(rate)} 低于历史可取范围下限 {pct(lo)}"], hist, cur)
    if hi is not None and rate > hi:
        return Verdict("未见改善", [f"现行超时率 {pct(rate)} 高于历史可取范围上限 {pct(hi)}"], hist, cur)
    if lo == hi:
        return Verdict("未见改善", [f"现行超时率 {pct(rate)} 与历史水平 {pct(lo)} 持平"], hist, cur)
    reasons = []
    if hist.flex_attribution:
        reasons.append("归属未定：跨区街巷的未定位余数可在片区间移动")
    if hist.straddle:
        reasons.append("口径未定：历史等候档跨越承诺，只有区间")
    return Verdict("方向未定", reasons, hist, cur)


def rollup(verdicts):
    """片区层面的小结；不跨服务承诺合并数字。"""
    labels = [v.label for v in verdicts.values()]
    if not labels:
        return "无共同口径"
    if all(label == "确实改善" for label in labels):
        return "确实改善"
    if any(label == "确实改善" for label in labels):
        return "只有一部分比较可支持"
    if any(label == "未见改善" for label in labels):
        return "未见改善"
    return "口径或归属使方向未定"


# ---------------------------------------------------------------- 联合归属约束

def joint_constraints(buckets, service):
    """同一批未定位人数不得重复计入两个片区：单方上界与共同归属下的联合上界。"""
    out = []
    for b in buckets:
        if b.service != service or b.remainder == 0 or len(b.candidates) < 2 or b.status == "none":
            continue
        for d1, d2 in combinations(sorted(b.candidates), 2):
            l1, l2 = b.located.get(d1, 0), b.located.get(d2, 0)
            best = max(min(l1 + share, l2 + b.remainder - share)
                       for share in range(b.remainder + 1))
            out.append({
                "bucket": b, "pair": (d1, d2),
                "max": (l1 + b.remainder, l2 + b.remainder),
                "joint_min_max": best,
            })
    return out


def evidence_gaps(buckets, service):
    """指出需要补证的原始街巷与档：归属未定补定位记录，区间跨承诺补明细时长。

    按对结论的影响排序：先列影响超时分子的缺口，再列仅影响分母的归属缺口。
    """
    gaps = []
    for b in buckets:
        if b.service != service:
            continue
        if b.remainder > 0 and len(b.candidates) > 1:
            priority = 0 if b.status != "none" else 2
            gaps.append((priority,
                f"街巷 {b.lane} 档 {b.bucket} {b.band_text()}：余 {b.remainder} 人归属未定"
                f"（可能归 {'、'.join(b.candidates)}），需补定位记录"))
        if b.status == "straddle" and b.count > 0:
            gaps.append((1,
                f"街巷 {b.lane} 档 {b.bucket} {b.band_text()}：档跨承诺，仅知区间，"
                "需补明细时长（不得按区间中心补造）"))
    return [text for _priority, text in sorted(gaps, key=lambda item: (item[0], item[1]))]


# ---------------------------------------------------------------- 周末窗口保底

def plan_choices(windows):
    """承诺班次固定；候选班次按 group 分组，同组为同一值班资源的备选排法。"""
    announced, groups = [], {}
    for w in windows:
        if w["state"] == "announced":
            announced.append(w["shift"])
        elif w["state"] == "candidate":
            groups.setdefault(w["group"], []).append(w["shift"])
        else:
            raise ValueError(f"班次 {w['shift']}: 未知状态 {w['state']}")
    return announced, {g: sorted(ids) for g, ids in groups.items()}


def floor_needs(aid, services, hists, curs):
    """保底需求：帮扶范围内、可能至少有一件超时业务的 (片区, 服务)。

    取各共同归属情形下需求的并集——不允许用"对各区分别最有利"的归属
    组合来免除保底；同一批未定位人数不得重复计入两个片区的超标证明。
    """
    needs = {}
    for d in aid:
        for s in services:
            cur = curs.get((d, s))
            hist = hists.get((d, s))
            cur_timeout = cur.timeout if cur else 0
            hist_min = hist.min_timeout if hist else 0
            hist_max = hist.max_timeout if hist else 0
            if cur_timeout >= 1 or hist_max >= 1:
                needs[(d, s)] = {
                    "current_timeout": cur_timeout,
                    "history_range": (hist_min, hist_max),
                    "certain": cur_timeout >= 1 or hist_min >= 1,
                }
    return needs


def coverage(by_id, plan_ids, district, service):
    return sum(1 for pid in plan_ids
               if by_id[pid]["service"] == service
               and district in by_id[pid]["reaches"].split(";"))


def check_plan(by_id, plan_ids, needs):
    """保底：同一服务类型、不同地点的可到达时段可累计，须满两个。"""
    shortfalls = []
    for (d, s) in needs:
        n = coverage(by_id, plan_ids, d, s)
        if n < 2:
            shortfalls.append((d, s, n))
    return shortfalls


def evaluate_plans(windows, needs):
    """枚举排法：承诺班次不动，同组至多启用一个。返回 (announced, groups, valid)。"""
    by_id = {w["shift"]: w for w in windows}
    announced, groups = plan_choices(windows)
    options = [[None] + ids for _g, ids in sorted(groups.items())]
    valid = []
    for combo in product(*options):
        plan = announced + [c for c in combo if c]
        if not check_plan(by_id, plan, needs):
            valid.append(plan)
    return announced, groups, valid


def parse_plan(text, windows):
    """解析指定排法（候选班次编号）；承诺班次自动包含且不可移除。"""
    by_id = {w["shift"]: w for w in windows}
    announced, groups = plan_choices(windows)
    chosen = [part.strip() for part in text.split(",") if part.strip()]
    for pid in chosen:
        if pid not in by_id:
            raise ValueError(f"班次 {pid} 不存在")
        if by_id[pid]["state"] != "candidate":
            raise ValueError(f"班次 {pid} 是承诺班次，自动保留，无需也不能指定")
    seen = {}
    for pid in chosen:
        group = by_id[pid]["group"]
        if group in seen:
            raise ValueError(f"班次 {seen[group]} 与 {pid} 同属 {group}，是同一值班资源的备选，不能同时启用")
        seen[group] = pid
    return announced + chosen


# ---------------------------------------------------------------- 展示

def pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def render_report(args, tables, policy):
    lines = []
    mapping = mapping_at(tables["locations"], quarter_end(policy["current"]))
    services = list(policy["commitments"])
    commitments_text = "；".join(f"{s} 承诺 {m} 分钟" for s, m in policy["commitments"].items())
    aid = aid_districts(tables["locations"], args.aid_from)

    buckets, warnings = build_buckets(tables, mapping, policy["commitments"], policy["history"])
    hists = historical_by_district(buckets)
    curs, cur_warnings = current_by_district(tables, mapping, policy["commitments"], policy["current"])
    warnings += cur_warnings

    lines.append("== 区划调整后的等待公平比较 ==")
    lines.append(f"比较期间：历史 {policy['history']} → 现行 {policy['current']}"
                 f"（楼栋对照按生效日期，取截至 {quarter_end(policy['current'])} 的现行归属）")
    lines.append(f"共同服务口径：{commitments_text}（{policy['raw']['超时定义']}）")
    lines.append(f"帮扶范围：原片区 {'、'.join(args.aid_from)} → 现行片区 {'、'.join(aid) or '（无）'}"
                 "（可用 --aid-from 调整）")
    for text in warnings:
        lines.append(f"提示：{text}")
    lines.append("")

    show_districts = args.districts or sorted({d for d, _s in set(hists) | set(curs)})
    show_services = args.services or services
    verdicts = {}
    for d in show_districts:
        district_verdicts = {}
        for s in show_services:
            hist, cur = hists.get((d, s)), curs.get((d, s))
            verdict = compare(hist, cur)
            verdicts[(d, s)] = verdict
            district_verdicts[s] = verdict
            lines.append(f"-- {d} · {s} --")
            if hist is not None and hist.max_total:
                lines.append(f"历史超时率可取范围：{pct(hist.rate_lo)} – {pct(hist.rate_hi)}"
                             f"（超时 {hist.min_timeout}–{hist.max_timeout} 件 /"
                             f" 总数 {hist.min_total}–{hist.max_total} 件）")
                for b, certain, flex in sorted(hist.bands, key=lambda item: item[0].bucket):
                    note = {"all": "整档超时", "none": "整档未超时",
                            "straddle": "档跨承诺，超时人数未定"}[b.status]
                    flex_text = f"，另有多至 {flex} 人随余数归属" if flex else ""
                    lines.append(f"  档 {b.bucket} {b.band_text()}（{note}）：确切 {certain} 人{flex_text}")
            else:
                lines.append("历史：无可归属记录")
            if cur is not None and cur.done:
                lines.append(f"现行：完成 {cur.done} 件，超时 {cur.timeout} 件（{pct(cur.rate)}）；"
                             f"中断 {cur.interrupted} 件另登记，不计入已完成时长")
                lines.append(f"  现行明细（已完成分钟）：{'、'.join(str(m) for m in cur.minutes)}")
            else:
                extra = f"（另有中断 {cur.interrupted} 件）" if cur and cur.interrupted else ""
                lines.append(f"现行：无已完成明细{extra}")
            lines.append(f"结论：{verdict.label}")
            for reason in verdict.reasons:
                lines.append(f"  - {reason}")
        lines.append(f"片区小结（{d}）：{rollup(district_verdicts)}（不跨服务承诺合并）")
        lines.append("")

    lines.append("== 联合归属约束 ==")
    lines.append("同一批未定位人数不得重复计入两个片区；"
                 "不提供把各片区各自最有利数字拼接而成的全局结论。")
    any_constraint = False
    for s in show_services:
        for item in joint_constraints(buckets, s):
            any_constraint = True
            b, (d1, d2) = item["bucket"], item["pair"]
            m1, m2 = item["max"]
            lines.append(
                f"档 {b.bucket}（{b.lane}，{b.band_text()}，{s}）：余 {b.remainder} 人可在 "
                f"{d1}、{d2} 间移动；单方上界 {d1}≤{m1}、{d2}≤{m2} 不能同时成立，"
                f"共同归属下较小一方至多 {item['joint_min_max']} 件")
    if not any_constraint:
        lines.append("（本期没有跨区未定位余数）")
    lines.append("")

    lines.append("== 周末窗口保底核查 ==")
    lines.append(f"保底定义：{policy['raw']['保底定义']}")
    needs = floor_needs(aid, services, hists, curs)
    by_id = {w["shift"]: w for w in tables["windows"]}
    if not needs:
        lines.append("帮扶范围内没有可能超时的 (片区, 服务)，本期无保底需求。")
    else:
        for (d, s), info in sorted(needs.items()):
            lo, hi = info["history_range"]
            source = "需求确定" if info["certain"] else "需求来自未定归属/区间，补证后可能解除"
            lines.append(f"  {d} · {s}：现行超时 {info['current_timeout']} 件，"
                         f"历史可能超时 {lo}–{hi} 件（{source}）→ 须有两个可到达时段")
    announced, groups, valid = evaluate_plans(tables["windows"], needs)
    lines.append(f"承诺班次（保留承诺、不移动）：{'、'.join(announced)}")
    if valid:
        minimal_len = min(len(plan) for plan in valid)
        minimal = [plan for plan in valid if len(plan) == minimal_len]
        for plan in minimal:
            chosen = [pid for pid in plan if pid not in announced]
            lines.append(f"可行排法：承诺 {'、'.join(announced)} + 启用 {'、'.join(chosen)}")
        extras = sorted({pid for plan in valid for pid in plan}
                        - set(announced) - {pid for plan in minimal for pid in plan})
        if extras:
            lines.append(f"另可任选：{'、'.join(extras)}")
    else:
        lines.append("没有任何排法能满足保底，需增设时段或先补证缩小需求。")
    for group, ids in sorted(groups.items()):
        for pid in ids:
            if any(pid in plan for plan in valid):
                continue
            mates = [other for other in ids if other != pid]
            others = [c for g2, ids2 in groups.items() if g2 != group for c in ids2]
            reasons = []
            for (d, s) in needs:
                base = coverage(by_id, announced + others, d, s)
                with_mate = base + max((coverage(by_id, [m], d, s) for m in mates), default=0)
                with_pid = base + coverage(by_id, [pid], d, s)
                if with_mate >= 2 > with_pid:
                    reasons.append(f"{d} · {s} 将只剩 {with_pid} 个可到达时段")
            detail = "；".join(reasons) if reasons else "不满足保底"
            lines.append(f"不可行调法：{pid}（{by_id[pid]['site']}）替代 "
                         f"{'、'.join(mates)} → {detail}，失去保底")
    if args.plan:
        try:
            plan = parse_plan(args.plan, tables["windows"])
        except ValueError as error:
            lines.append(f"指定排法无效：{error}")
        else:
            shortfalls = check_plan(by_id, plan, needs)
            if shortfalls:
                text = "；".join(f"{d} · {s} 仅 {n} 个可到达时段" for d, s, n in shortfalls)
                lines.append(f"指定排法 {'、'.join(plan)}：不满足保底（{text}）")
            else:
                lines.append(f"指定排法 {'、'.join(plan)}：满足保底")
    lines.append("")

    lines.append("== 结论 ==")
    aid_verdicts = {(d, s): verdicts.get((d, s)) or compare(hists.get((d, s)), curs.get((d, s)))
                    for d in aid for s in services}
    blocking = [(d, s) for (d, s), v in aid_verdicts.items() if v.label != "确实改善"]
    if blocking:
        text = "、".join(f"{d} · {s}（{aid_verdicts[(d, s)].label}）" for d, s in blocking)
        lines.append(f"原帮扶片区的等待差距未被证明消除：{text}。")
        lines.append("不支持以“已经改善”为由迁出周末窗口；如仍需调整，须满足上方保底约束。")
        outside = [(d, s) for (d, s), v in verdicts.items()
                   if d not in aid and v.label == "确实改善"]
        for d, s in outside:
            lines.append(f"注意：{d} · {s} 确实改善，但 {d} 不在帮扶范围，"
                         "不能据此为帮扶片区下结论（各片区数字不拼接）。")
        lines.append("需补证：")
        seen = set()
        for _d, s in blocking:
            for gap in evidence_gaps(buckets, s):
                if gap not in seen:
                    seen.add(gap)
                    lines.append(f"  - {gap}")
    else:
        lines.append("帮扶范围各片区、各共同口径服务均确实改善；"
                     "迁移周末窗口仍须采用上方可行排法，保持保底开放机会。")

    if args.published_before:
        lines.append("")
        lines.append(f"== 已公开口径（截至 {args.published_before}，冻结，不随新确认归属改写） ==")
        old_buckets, _ = build_buckets(tables, mapping, policy["commitments"],
                                       policy["history"], received_before=args.published_before)
        old_hists = historical_by_district(old_buckets)
        for d in sorted({d for d, _s in old_hists}):
            for s in services:
                h = old_hists.get((d, s))
                if h is None or not h.max_total:
                    continue
                lines.append(f"  {d} · {s}：历史超时率可取范围 {pct(h.rate_lo)} – {pct(h.rate_hi)}"
                             f"（超时 {h.min_timeout}–{h.max_timeout} 件）")
        new_records = [row["record"] for row in tables["located_counts"]
                       if row["received"] >= args.published_before]
        if new_records:
            lines.append(f"新确认归属（{args.published_before} 起收到）：{'、'.join(new_records)}"
                         "——仅影响当前分析与尚未通知的迁移决定，不改写上方已公开口径。")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data", help="交接资料目录")
    parser.add_argument("--aid-from", nargs="+", default=["west"],
                        help="原帮扶片区（locations 的 district_from），默认 west")
    parser.add_argument("--published-before", default=None,
                        help="复现已公开口径：只用该日期前收到的定位记录（冻结，不改写）")
    parser.add_argument("--plan", default=None, help="核查指定排法，逗号分隔的候选班次编号")
    parser.add_argument("--districts", nargs="+", default=None, help="只比较指定现行片区")
    parser.add_argument("--services", nargs="+", default=None, help="只比较指定服务")
    args = parser.parse_args(argv)

    tables = load_tables(args.data)
    policy = load_policy(args.data / "policy.json")
    print(render_report(args, tables, policy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
