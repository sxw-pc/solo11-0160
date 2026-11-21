# 社区避难点启用采录台

离线的避难点资源、灾情启用与分流、人员入离及配给业务账。设施失效会缩减实际床位并为已收容者生成待转移决定；矛盾启用令会冻结新增收容；跨点重复入场和重复领物资会拒绝。原始文件字节、事件哈希与裁定版本持久化在 SQLite。

在原有三套总量账之上，本版增加**证据台**：离线床卡、换床、出点车闸时窗、到点接收时窗与现场物资签领采录，按原始时刻与见证关系重建逐床的在用 / 待核 / 预留 / 故障区间，而不是用一张转移计划单提前释放床位。

## 证据模型

* **原件层（append-only，可撤证）**：`bed_card` 床卡独立编号、`bed_fault`/`bed_repair` 只关闭或重开涉及的床号、`bed_assign`/`bed_move` 分床与换床、`gate_record` 出点车闸时窗、`arrival_record` 到点接收时窗、`transfer_plan` 转移计划、`supply` 本人签领、`evidence_revocation` 撤证、`admission_request` 准入申请。
* **重裁层（每批原件落定后整体重建，一批原件恰好一版）**：
  * `journey` 逐人可行行程集合——计划转移、实际离点、途中、接收完成是不同事实；离点/到达都是时窗，多份原件取时窗交集。
  * `bed_interval` 逐床区间：`occupied` 所有行程占用、`unverified` 仅部分行程空（待核）、`vacated` 所有行程均已离床、`reserved` 计划预留、`fault` 故障。
  * `bed_verdict` 目标时刻逐床三态：`free`（所有行程均空）/ `partial`（仅部分行程空）/ `occupied`（所有行程均占用），另有 `unsafe`。
  * `admission_decision` 两份已签用途合同的独立裁定；`reversal` 对旧结论（含旧总量排序结论）的撤回记录；`report_snapshot` 留存每版报表，旧版本可查。
* **两份合同的区别**：方案甲（`A-realtime` 现场实时准入）只有一张床在所有相容行程中都空且安全才签实际入场，否则明确待核；方案乙（`B-contingency` 跨点预案调配）只要存在一条相容行程使该床在目标时刻空，就列为有条件候选，候选不等于已收容。同床同时刻两合同决定不同时，合同号与条件都写入裁定。
* **见证约束**：本人签领本点物资要求在场，给实际离点时刻加下界；到点接收给到达时刻加上界；两点时刻与车辆出入必须可行先后。原件无法组成完整行程时给出最小冲突证据，不默默挑选单据。
* **配给额度**：每人每事件每种物资两份，跨点、跨行程累计；转移计划与另点重新入场都不清零已领配给。
* **故障对象**：床卡设施只按床号转移实际落在故障床上的人；没有床号的 `beds_lost` 总量账仍保留旧排序结论作为可撤回痕迹，但不能决定该转谁。

## 用法

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m shelterledger demo --db demo.sqlite
python -m shelterledger audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个导入入口仍是 `shelter-import`、`activation-import`、`intake-import`，用 `--file fixtures/*.json` 喂原件；床卡、床级故障、车闸、接收、转移计划、撤证、准入申请等新事件按子系统分发到对应入口。每批原件导入后自动重裁一版。

证据台的离线演示与查询：

```powershell
python -m shelterledger offline-demo --db evidence.sqlite
python -m shelterledger verdicts --db evidence.sqlite --request REQ-A-1015 [--version N]
python -m shelterledger journeys --db evidence.sqlite --person A [--version N]
python -m shelterledger snapshot --db evidence.sqlite --version 6
```

`fixtures/evidence/` 是一组逐秒可核的合成单据，覆盖：计划未离点不释放床位、10:15 两合同分歧、独立视频收紧离点上界后保证空床、到点撤证退回在途、晚到修复重开床号、四床反例（旧排序误标 C4，床卡证据改标 A4 并撤回 C4）、无法闭合行程的最小冲突证据，以及配给跨点保留。旧的确定时刻入离记录仍可正常读取，但不会生成逐床区间、不冒充已证明的逐床位置。所有身份、签章和社区名称为合成数据，首版不连真实指挥系统。
