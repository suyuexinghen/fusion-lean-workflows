---
name: fusion-lean-workflow
description: 在 Fusion Lean 中编排任务、确认前置和独立评审约束、用固定 Git 证据交接，并对照阶段健康报告。适用于接手或设计 Lean、数值计算、文献、Wiki 和复验工作。
---

# 接手 Fusion Lean 的一个阶段

1. **定位目标和队列。** 查该阶段的终点、共同队列、各 Git 仓库根、冻结证据与上一轮断点。用 `list`、`show`、`history` 确认任务、归属和在等哪些前置。新项目可先跑[合成队列演练](references/demo.md)。
2. **把接收方和验证写进任务图。** 用 `needs` 表达流程依赖，用 `independent_of` 指出独立评审涉及的前置，用 `resources` 登记共享写入。验收指明下游实际入口、至少一个能暴露错误的用例和应交的 Git 路径。跨仓任务按[四仓交接](references/compute-workflow.md)组织。
3. **领取与执行。** 用稳定的会话代号 `claim`，打开返回的 `role_skill`。独立作答者先收中性题面和冻结来源，固定答案后再看产物、旧卡与前次裁决。[岗位与盲答交接](references/role-practice.md)
4. **交付、换人或退修。** `done` 登记实得及 `仓库@完整提交:路径`。需要换人时用 `handoff` 留下最后通过的检查、首个失败与下一动作。发现实质差异用 `revise` 开修订依赖，为终稿另绑定裁决。[队列命令与状态](references/queue.md)
5. **阶段收口。** 实测结果、独立审读、真人反馈和具名签审分别记录。按终点检查仍缺哪类证据，然后用 `health` 对照前次积压、交付与复发，登记下一次辨别试验。[健康观察](references/health.md)

已有长程目标时，按 [Goal 技能](../fusion-lean-goal/SKILL.md)判断后继批次。要从来源选新题或改变验收方法时，用[跨轮迭代](../fusion-lean-evolve/SKILL.md)。需要迁移到其他领域时，从[通用方法](../evidence-driven-workflows/SKILL.md)选取同样的阶段结构。
