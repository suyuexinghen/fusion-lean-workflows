# Fusion Lean Workflows

用可运行的任务图、固定 Git 证据与独立评审，组织多人或多 agent 的长期协作。这里有两套可以独立采用的技能。

| 入口 | 适用场景 |
|---|---|
| [evidence-driven-workflows](skills/evidence-driven-workflows/SKILL.md) | 通用方法。从使用者的问题倒推原始证据、验收和交接，再用另一份输入检验工作方法。 |
| [fusion-lean-workflow](skills/fusion-lean-workflow/SKILL.md) | Fusion Lean 的项目实例。用 SQLite 任务图连接来源、Lean、数值计算、Wiki 与独立回验。配有可运行的队列及 [11 个岗位与配套技能](skills/fusion-lean-workflow/references/role-practice.md)。 |

## 十分钟复跑

需要 Python 3.10+ 和 Git。完整回归含使用 POSIX 文件锁的续派程序，请在 Linux 或 macOS 运行。两个演练使用合成输入，可离线执行。

```sh
git clone https://github.com/suyuexinghen/fusion-lean-workflows.git
cd fusion-lean-workflows
python3 skills/evidence-driven-workflows/examples/replay.py
python3 skills/evidence-driven-workflows/examples/replay.py --compare-gates
python3 skills/fusion-lean-workflow/examples/replay.py
python3 -m unittest discover -s skills/fusion-lean-workflow/tests -v
```

第一个演练用两个供应方演示合法零值与未上报的区别。旧规则在两个输入上都失败，新规则都通过。第二条比较同一份输出在结构检查和合同检查中的裁决。第三条在临时目录创建 Git 证据与队列，演示并行任务汇合、实际换人、拒绝作者自审、退修和再审。可按 [队列演练](skills/fusion-lean-workflow/references/demo.md) 阅读每个产物和事件。

## 在 Claude Code 或 Codex 中使用

从本仓游览或复制技能目录。只使用通用方法时复制 `skills/evidence-driven-workflows/`。使用 Fusion Lean 实例时复制 `skills/` 的所有子目录，让岗位技能保持同级。目标应用的项目目录为 Claude Code 的 `.claude/skills/` 或 Codex 的 `.agents/skills/`。已有同名技能时先比较内容。

| 客户端 | 通用方法 | Fusion Lean |
|---|---|---|
| Claude Code | `/evidence-driven-workflows` | `/fusion-lean-workflow` |
| Codex | `$evidence-driven-workflows` | `$fusion-lean-workflow` |

例：在新项目运行「用 evidence-driven-workflows，为 CSV 导入设计第一轮验收，并跑通合法零值和缺失值的对照」，或打开 `fusion-lean-workflow` 追踪队列演练中哪几个任务解锁独立审读。

## 发布范围

通用方法与队列源于 Fusion Lean 的实际协作。公开版保留结构、判断规则与经改写的经验卡，用合成任务、输入和现场标识示范。读者可使用自己的论文、仓库和验收记录接入同一流程。项目工作站、生产资料库、内部版本历史、会话与队列快照留在原环境。[公开案例说明](skills/evidence-driven-workflows/references/fusion-case.md)
