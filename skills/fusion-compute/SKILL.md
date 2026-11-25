---
name: fusion-compute
description: 用聚变科研软件生成、导出与重放数值结果，核对模型约定、误差和带错对照，并交付固定运行记录。
---

# compute · 交付可复跑的数值结果

1. 与使用者指定物理量、模型前提、单位、输入和要作的判断。用 C 的独立预期或解析例定好可区分错误的输入和容差。
2. 在公开入口运行。固定真实加载的版本、命令、输入摘要及网格、步长等相关配置。分别检查坐标与单位、求解误差和导出重采样；记录覆盖域、读数、阈值和首个失败。
3. 给 evidence 固定运行记录与复跑方法，给 editor 可引用的数值和适用范围。出现一般性反例时将输入和差异交给 B。运行记录字段见[四仓交接](../fusion-lean-workflow/references/compute-workflow.md)。

任务的领取、交付和所有权见[队列手册](../fusion-lean-workflow/references/queue.md)。
