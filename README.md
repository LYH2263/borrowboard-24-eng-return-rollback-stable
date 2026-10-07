# Borrowboard · 邻里借用

上架 → 借出通过 → 归还；单物件同时仅一笔在借，含逾期判定。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5400 |
| API | 10400 |

0-1：`deposit` / `damage_note` / `neighbor_rating`。

## 半插入残局补偿（命令入口）

借出流程若在 `items` 已置 `on_loan`、`loans` 行未落成时崩溃，会留下残局。
前端借还入口不处理残局，统一由命令入口处置（注入钩子 / 扫描 / 报告三分开，
不进业务路由）：

```bash
# backend/ 目录下
python -m app.recovery.cli scan                      # 只读扫出残局 item_id
python -m app.recovery.cli inject <item_id>          # 演练钩子：制造一个残局
python -m app.recovery.cli reconcile                 # 补一行 active（默认）
python -m app.recovery.cli reconcile --strategy reset_available  # 或改回 available
```

补偿在 `BEGIN IMMEDIATE` 写事务内、锁内复检后落地，与再借/归还串行共处：
不会补出双 active，也不会把尚有 active 行的物刷回 available。

退出码：`0` 成功（含第二次连跑的空名单）；`2` 对账失败（未收敛 / 拿不到写锁），
不与成功混用；`1` 用法或注入错误。

