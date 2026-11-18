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
python3 -m unittest -v
```
