# 技能适用判断与预算内动作纠错

默认开启 `EVOCODER_SKILL_ROUTING=semantic` 和 `EVOCODER_RECOVER_ACTIONS=true`。现有 SQLite 状态无需清空，新配置和实际使用的技能保存在每次运行里。

## 技能适用判断

路由模型只看到问题描述、技能 applicability 和 exclusions，不看修复步骤、仓库历史成绩和隐藏测试。它输出 apply / skip / uncertain、条件/禁用条件索引、问题原文引用及简短理由。apply 必须有合法的适用条件索引及原文引用，没有阻断条件；不确定或结构/引用非法时显式跳过技能，继续无技能求解。

这不是关键词规则，也没有硬编码某个仓库的正确答案。语义判断仍可能错误，原文引用校验不构成正确性证明。当前只筛选一个请求的技能，没有多技能召回与排序。

`runs.skill_id` 与 `result.applied_skill_id` 记录实际注入的技能；`result.requested_skill_id` 记录请求的版本。SKILL_ROUTE 保存路由理由、输入和提示哈希。普通运行的候选审批规则不变，跳过路由不能绕过版本验证。未实际注入某父技能的任务失败不能用于声明“父技能应用失败”，自动 stream 会跳过这次父版本修订。

## 动作参数纠错

收到完整且已结算的 `submit_action` 后，如果参数未通过校验，例如 line_count=240 超过 200，原动作不会执行，也不将 240 偷改为 200。运行器记录原始输出产物、校验错误和 ACTION_REJECTED，再把错误反馈给下一次模型决策。模型必须自己给出合法动作。

每次被拒绝的决策都消耗原有模型步骤。16 步不会变成“16 个有效工具调用再加无限纠错”。一次语义路由也占一步，开启路由时最多再调用 15 次求解模型。`model_steps` 是模型决策尝试次数，`steps` 是已执行的普通工具次数（不含 finish），`rejected_actions` 单独记录；实际付费请求次数与费用仍以账本为准。

HTTP 超时或不完整计费不会触发自动重发。2026-11-17 起，完整 HTTP JSON 且 usage 已结算的响应中，零个/多个调用、错误函数名或输出被长度上限截断的不可用决策，也会拒绝执行并反馈，模型可在原步数内重新决策；不会把 search 等函数名自动当作合法 submit_action 执行，也不补全截断参数。HTTP JSON 不完整、usage 不可用或传输不确定仍终止。路由的已结算结构/语义/函数错误会显式跳过记忆，不重新调用路由；传输错误则终止，保留未结算预留。合法结构的越权写入仍被网关拒绝，不因为开启纠错而执行。底层命令超时仍销毁容器。

真实适配器遇到过长输入时，会先对已记录的公开历史作确定性截断投影，标注原始哈希、保留范围和截断级别；不会改写实际工具日志或候选证据。问题、记忆和工具合同仍受输入上限约束，无法缩减到限制内时不发出请求。四组对照使用相同策略，详见 [实验协议](comparison-protocol.md)。

## 使用与对照开关

```bash
# 新默认配置：仅在适用时注入技能，动作参数可在原预算内纠正
uv run --locked evocoder --skill-routing semantic --recover-actions run TASK.json

# 对照旧注入/终止策略；仍使用当前运行器与同一独立评分器
uv run --locked evocoder --skill-routing off --no-recover-actions run TASK.json
```

单次任务必须使用合适的实验镜像；真实仓库准备见 `realworld.md`。关闭路由代表直接注入所选技能，不代表自动获得候选使用权限。

`scripts/run_routing_recovery.py` 冻结现有 v2，对两道已见 Click dev 做路由/纠错 2×2 组合及无技能控制，另有空配置缓存的正向适用探针。相同模型总步骤预算计入路由开销；不修改技能、不发布、不重跑旧终评。该脚本用于本次固定消融，默认写到新的 `experiments/optimization-001`，存在协议或启动标记时拒绝重复执行。

```bash
uv run --locked python scripts/run_routing_recovery.py
uv run --locked python scripts/run_routing_recovery.py --run
```

相同模型、temperature=0 仍可能产生不同轨迹。每格仅一次运行，且案例已用于开发；参数纠错若未触发，不能把通过率差异归因于纠错。受控测试验证机制，真实模型对照验证观察到的行为，两者都不等于技能自进化的能力提升。
