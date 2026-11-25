---
name: fusion-lean-formalize
description: 用于 Fusion Lean 的 B 会话实现定义、证明、反例或陈述型接口，交付 Lean 签名、公共消费例及第一个未解目标。
---

# B · 交付一个可消费的 Lean 声明

1. 冻结原断言、拟议签名、量词、物理前提和实际消费入口。把证明拆到可编译的最小步骤，按固定 Mathlib 版本记录所用引理。
2. 在满足原前提的输入上找反例。若新增条件后才成立，保留原题的独立裁决，为修正版另立声明。对 S 类陈述登记已陈述的定义域，对 P 类证明附从公共入口调用的探针和公理检查。
3. 把签名、版本、一般调用、实例和首条诊断交给 A。两份具体输入带来的证据范围见[公共消费例](references/experience/general-consumer.md)。

任务的领取、交付和所有权见[队列手册](../fusion-lean-workflow/references/queue.md)。
