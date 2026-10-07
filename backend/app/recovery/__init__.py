"""半插入残局的工程化补偿：注入钩子 / 扫描补偿引擎 / 报告 三件分离。

- inject.py：制造残局的测试钩子，独立于借出业务路由
- reconcile.py：扫描 items.on_loan 但无 active loan 的残局并落地补偿
- report.py：把扫描/补偿结果渲染成文本或 JSON
- cli.py：命令入口（scan / reconcile / inject），前端借还入口不经过这里
"""
