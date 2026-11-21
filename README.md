# 社区避难点启用采录台

离线虚构的避难点资源、灾情启用与分流、人员入离及配给三套业务账。设施失效会缩减实际床位并为已收容者生成待转移决定；矛盾启用令会冻结新增收容；跨点重复入场和重复领物资会拒绝。原始文件字节、事件哈希与裁定版本持久化在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m shelterledger demo --db demo.sqlite
python -m shelterledger audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口是 `shelter-import`、`activation-import`、`intake-import`，用 `--file fixtures/*-ok.json` 和对应 `*-bad.json`。`report` 输出资源容量、事件令、目的地、入离、配给及转移记录；`audit` 从原始文件重放到新库逐表核对。所有身份、签章和社区名称为合成数据，首版不连真实指挥系统。

## 空笼成对笼牌采录（`carrier-import`）

`carrier-import` 原有一个 `label-read` 读档器：某台位一次可见码集合为单个时签套 `ready`，多个时 `needs_capture`，保持不变。新增的是两个空笼同托盘成对采录，事件类型为 `pair-round`、`pair-image`、`split-start`、`split-interrupt`、`split-receipt`，样例见 `fixtures/carrier-pair-ok.json` 与 `fixtures/carrier-pair-bad.json`。

规则（全部由 `shelterledger/carrier_pair_capture.py` 落库并重放）：

- 每图有多条页读码（`outer` 外页、`inner_reflection` 内页反光）。只有固定左右相机的 `outer` 读数能把码绑到实际位置；反光和画面内标签顺序永不定位。
- 同一轮必须共享同一个回转停点；左右两路来自不同停点的截图不能凑成镜向的一帧，直接拒绝。
- 同停点见证下左右各读到唯一外码时可出两张签套；一侧过曝或缺读时只出可证的那一张，另一张标为 `unconfirmed`，附候选码与剩余图，不替它补边。
- 已发签套与位置是单向锁：晚到图只作为证据登记，若会改写已发码或把码挪到另一边则被拒绝；不同轮次的签套不能被互补拼成一对。
- 分离采录只在「共同归向见证丢失、尚未发任何签套」时可做，全站只允许一次可能改动托盘的分离与一次中断。中断后不得凭电子状态无条件重分，也不得补记实体回执。
- 分离后实体封套回执锁定一个位置，`split-receipt` 只接受与回执串号/外码一致的封套图，且只出这一张；另一位置保持未确定，原同盘两路联动的来源已不可补拍。

限额：两个模型笼、两轮、一次中断、四张标签原图。只读查看用 `carrier-receipt --db <db> [--round R1]`，同时给出签套回执与「下一步在物理上仍相容的首动作」。

