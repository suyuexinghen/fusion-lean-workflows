# 用固定回执接续阶段

批次结束后，由协调者在固定回执中写清后续事项，避免新会话重复造图。程序可重复读取同一 Git blob。使用 `fusion-task successors REPO@SHA:PATH --host NAME --session COORDINATOR` 查看或装入授权范围内的后继项。

回执含 `version: 1` 和 `decisions`。每项填写 `host`、`goal`、`work_key`、`result_id`、`finding`、`acceptance`、`evidence`、`action`。动作分别为：

- `reuse` 关联既有任务，`load` 首次装入固定 `plan`。两者给出 `task_ids`。需要跨批前置时给 `after`。
- `wait_external` 给 `resume_when`。材料补齐后，后继项引用新 `recovery_evidence`。
- `close` 记录当前终点，`needs_decision` 说明需要决定的事项。

`dispatch` 固定任务修订、接收方和工作键；接收方用 `start` 加派发事件序号确认。确认可重试。重复回执会核对工作键与既有任务的绑定。实际已经在途、仍等待、投递待确认分别从队列读取。

需要自动投递时可用同一技能下的 `scripts/fusion-continue --help`。协调者自行提供受信任的 routes JSON，键为任务 ID，值含 `work_key`、`session` 与明确的 `command` 参数数组；接收程序通过 `FUSION_CONTINUATION` 读取派发编号和队列参数。它使用 POSIX 锁。普通演练可直接用队列命令。
