# 社区避难点启用采录台

离线虚构的避难点资源、灾情启用与分流、人员入离及配给三套业务账。设施失效会缩减实际床位并为已收容者生成待转移决定；矛盾启用令会冻结新增收容；跨点重复入场和重复领物资会拒绝。原始文件字节、事件哈希与裁定版本持久化在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m shelterledger demo --db demo.sqlite
python -m shelterledger audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口是 `shelter-import`、`activation-import`、`intake-import`，用 `--file fixtures/*-ok.json` 和对应 `*-bad.json`。`report` 输出资源容量、事件令、目的地、入离、配给及转移记录；`audit` 从原始文件重放到新库逐表核对。所有身份、签章和社区名称为合成数据，首版不连真实指挥系统。
