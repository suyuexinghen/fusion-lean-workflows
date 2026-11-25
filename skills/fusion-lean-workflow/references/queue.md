# 队列命令与状态

本技能的 `scripts/fusion-task` 是 Python/SQLite 工具。选择同一台机器上的数据库路径和 Git 检出父目录，让所有执行会话使用同一参数。下例从本技能目录执行，使用演练用临时队列。

```sh
scratch=$(mktemp -d)
task() { python3 scripts/fusion-task --db "$scratch/queue.sqlite3" --repos "$scratch" "$@"; }
task init plans/demo.json
task list --goal demo
task show DEMO-JUDGE
task claim DEMO-SOURCE --role C --session source
task history DEMO-SOURCE
```

`init` 幂等装入初始图。已有队列用 `load <plan.json> --session <coordinator>` 增加批次。图的格式是 `{"version":1,"tasks":[...]}`。必填字段为 `id`、`goal`、`stage`、`role`、`title` 和 `acceptance`。可选字段有 `priority`、`needs`、`independent_of`、`resources` 和 `artifacts`。

| 命令 | 队列提供的约束 |
|---|---|
| `next`、`list`、`show` | 查看同会话在途任务、就绪项、等待的前置和资源持有者。 |
| `claim --role R --session S` | 原子领取一个就绪项或返回本人在途项。一个会话最多持有一个任务。多个会话可并行领取无冲突项。 |
| `done ID --session S --rev N --note TEXT --evidence REPO@SHA:PATH` | 用当前修订号完成。引用必须指向已登记仓库中可读的 Git blob，并满足计划要求的产物模式。 |
| `handoff ID --to S2 --session S --rev N --note TEXT` | 交换当前所有者，保留两端会话与断点。已放弃的在途任务可由协调者用 `recover` 记录接管理由。 |
| `revise ID --fix FIX --fix-role R --acceptance TEXT --session S --rev N --note TEXT` | 创建修订任务，原审读等待修订，修订作者与复评者受独立性约束。 |
| `block`、`unblock`、`release` | 记录外部恢复条件或释放任务，后续可从历史恢复。每次写入使用最新修订号。 |
| `history`、`snapshot`、`backup` | 观察事件顺序、导出板和事件、备份 SQLite。控制快照的访问范围，因为它保留任务题面、会话代号和所有证据引用。 |

四个证据仓名及用途见[四仓交接](compute-workflow.md)。工具核查 Git 对象和声明过的会话冲突，协调者另外核实角色是否由不同实际执行者承担、产物内容是否成立。实际消费须以使用方的固定记录确认为准。[队列演练](demo.md)给出完整轨迹和缺证示例。

阶段之间若要冻结后继批次和接收确认，见[跨批回执](continuation.md)。任务图、接口或体验存在重复返工时，把复现命令、预期、实得和下游影响交协调者，再按[健康观察](health.md)验证。
