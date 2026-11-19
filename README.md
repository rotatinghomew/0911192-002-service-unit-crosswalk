# 区划调整后的便民服务公平比较

街道服务窗口的区划沿革、旧办理汇总和现行值班资料。
这里的记录全部为合成资料。日期按当地日历解释，未附时区的时间使用东八区。

## 交接资料

CSV 使用 UTF-8 和逗号分隔。值留空表示这条资料未给出该字段，不等于零。
`data/schema.json` 登记表头与跨表编号关系；`data/policy.json` 保存本期业务配置。

- `data/locations.csv`：一栋楼的一次新旧归属转换；楼栋范围确切，街巷可跨区。 字段：`building`, `lane`, `district_from`, `district_to`, `effective`。
- `data/legacy_bins.csv`：街巷级总数已经包含随后提交的定位抽核记录，不得再并入合计。单位分钟。 字段：`bucket`, `quarter`, `lane`, `service`, `lower`, `upper`, `count`。
- `data/located_counts.csv`：历史办件抽录中，能确认在特定楼栋办理的档内人数。 字段：`record`, `bucket`, `building`, `count`, `received`。
- `data/service_events.csv`：interrupted 指按原约定离开队列并重新排号，不纳入已完成时长。 字段：`case`, `quarter`, `building`, `service`, `minutes`, `state`。
- `data/windows.csv`：reaches 用分号列可覆盖的现街区；承诺行不能移动；同 group 为同一值班资源的备选排法。 字段：`shift`, `site`, `reaches`, `service`, `state`, `group`。

## 资料校验

使用 Python 3.11.2 标准库，不需要安装扩展包。资料入口只核查交接格式，不计算业务结果。

```sh
python3 inspect_inputs.py
python3 -m unittest discover -s tests -v
```

## 公平比较与保底核查

`analyze.py` 按共同服务口径比较历史（2025-Q3 街巷档）与现行（2026-Q3 楼栋明细）的等待分布和超时率，并核查周末窗口排法。口径规则：

- 能定位到楼栋的记录先从街巷档总数中确切扣除；余数不按今天的全体人口比例分派，只施加归属限制；同一批未定位人数不能同时记给两个新片区。
- 历史等候档只有区间时保留区间，不按区间中心补造明细；业务类型只在可比服务承诺内比较；预约变更造成的中断另有登记，不计入已完成时长。
- 已对外通知的班次保留承诺、不移动；同 group 的候选班次是同一值班资源的备选排法，至多启用一个。
- 新确认的楼栋归属只影响当前分析与尚未通知的迁移决定；`--published-before` 复现的已公开口径保持冻结，不改写。

```sh
python3 analyze.py                                  # 当前口径完整报告
python3 analyze.py --aid-from west east             # 调整帮扶范围（默认原 west 片区）
python3 analyze.py --plan w2,w4,w7                  # 核查指定排法是否满足保底
python3 analyze.py --published-before 2026-10-01    # 附已公开口径（冻结）
python3 analyze.py --districts A B --services social  # 只比较指定片区与服务
```

帮扶范围默认取原 `west` 片区的承接街区（与已通知的西门班次覆盖 A、B 一致），如实际帮扶范围不同请用 `--aid-from` 指定。
