# Borrowboard · 邻里借用

上架 → 借出通过 → 归还；单物件同时仅一笔在借，含逾期判定。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5400 |
| API | 10400 |

0-1：`deposit` / `damage_note` / `neighbor_rating`。

## 借出半插入残局补偿

借出时若 `items` 已写成 `on_loan`、`loans` 插入失败，会留下"在借栏找不到、可借栏也借不到"的残局物件。前端借出入口保持不变，残局只由命令入口处理（三分开：`app/recovery/injection.py` 注入钩子、`scanner.py` 扫描、`report.py` 报告，`compensate.py` 落地，均不被业务路由导入）。

```bash
python -m app.cli scan                 # 只扫描，报告列出残局 item_id
python -m app.cli recover              # 落地补偿：把无 active loan 的 on_loan 物件改回 available
python -m app.cli inject <item_id>     # 演练：制造一笔半插入残局
# 均支持 --json。容器内：docker compose exec backend python -m app.cli scan
```

退出码：`0` 成功（账实相符或补偿完成，含空名单；连跑两次第二次仍为 0）；`1` 扫到可补偿残局；`2` 对账失败（双 active、available 挂 active、提交前复核不过），已整体回滚，绝不与成功 0 混用；`3` 用法错误 / 注入被拒。

补偿在单个 `BEGIN IMMEDIATE` 事务内拿写锁后二次扫描落地，条件 UPDATE 带 `NOT EXISTS(active)` 守卫并在提交前复核：不会在邻居借到后把物件改回 available，也不会产生双 active；不删 loans 行、不重建库。

