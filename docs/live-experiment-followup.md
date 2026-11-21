# EvoCoder 后续真实模型实验

本次分开保留严格反馈试验、一次引用失败、扩展反馈规则试验。固定模型权重，保持每题 16 步预算和同一个 1 USD 累计账本。所有任务为人工编写的缓存合同案例，不是自然仓库 issue 或 SWE-bench，开发者已知参考实现。

## 实际技能修订

- 父技能：`8e1999dda10186753f8eff01768cd9145bcab2fdab01fea35308f02f31478f80`
- 模型修订产物：`f3aac7a52daa7cb760c6864ef46666db2852e55f3d045e97fccc6065c1b960a0`
- 同步学习任务中，模型首次补丁在 invalidate-during-load 测试返回旧值 1，期望值为 2；随后修复通过。异步任务测试通过后继续改写三次，最终耗尽预算，没有提交补丁。
- 扩展后的反馈规则允许从这类已恢复的补丁错误和执行预算耗尽学习，但拒绝将改动前原始代码的失败当作技能失败。隐藏评分输出不进入提案。
- 新技能由真实模型完整生成，未人工修改；原文和逐字段差异见 candidate-v2.json。规则与接口修改是人工工程工作，不计为智能体自改。

## 开发集配对结果

| 任务 | 冻结父技能 | 修订技能 | 无技能控制 |
|---|---|---|---|
| dev-sync-condition | True | True | True |
| dev-async-global | False | False | False |
| dev-snapshot-alias | True | True | True |
| dev-keyed-revisions | True | True | True |

核心门禁：False；父技能 3/4，修订技能 3/4；无技能控制 3/4。

实际激活：False；当前 active：`无技能基线`。接纳还要求修订版不退化于无技能控制。没有放宽两个 dev 分组改善、零回归的原有质量门槛。

## 支持集回放与错误归因

| 任务 | 方案 | 状态 | 说明 |
|---|---|---|---|
| learn-sync-flight | 冻结父技能 | EXHAUSTED | 无运行异常；通过与否以最终独立验证为准 |
| learn-sync-flight | 修订技能 | ERROR | RuntimeError: command_timeout: sandbox destroyed |
| learn-async-flight | 冻结父技能 | FAILED | 无运行异常；通过与否以最终独立验证为准 |
| learn-async-flight | 修订技能 | ERROR | ValidationError: 1 validation error for Action |
| learn-ttl-policy | 冻结父技能 | COMPLETE | 无运行异常；通过与否以最终独立验证为准 |
| learn-ttl-policy | 修订技能 | COMPLETE | 无运行异常；通过与否以最终独立验证为准 |

门禁把 ERROR 保守归入 infrastructure_errors，其中本次实际原因分别是求解命令超时、模型动作 JSON 不合法，不能将它们描述为外部平台故障。它们也阻止发布，未自动重试或删除。

## 重复探针

下列两题已在严格试验运行过，因此是重复探针，不能称为未见终评。技能版本在探针开始前冻结，结果不写回。

| 任务 | 方案 | 状态 | 工具步骤 |
|---|---|---|---|
| test-identity-reset | no_skill | COMPLETE | 5 |
| test-identity-reset | frozen_seed | COMPLETE | 5 |
| test-identity-reset | revision | COMPLETE | 5 |
| test-ttl-negative | no_skill | COMPLETE | 4 |
| test-ttl-negative | frozen_seed | EXHAUSTED | 16 |
| test-ttl-negative | revision | COMPLETE | 4 |

## 严格规则与失败记录

严格试验运行状态：{'COMPLETE': 6, 'EXHAUSTED': 1}。严格规则没有生成新版本，因为最终公开测试均未保持失败，它忽略了中途错误和预算耗尽。这一负结果保持原样。

扩展反馈试验的第一次提案引用不属于冻结证据池，被拒绝，费用保留。原始候选正文当时未留存，不能倒推具体错误引用。随后完善显式引用 ID 枚举并另行冻结协议，只重试一次；新提案通过引用与父版本校验。当前已将未经语义校验的模型产物也保存为不可变产物。

收尾审查发现扩展策略仍可能让“仅原始坏代码测试失败、未改写”的运行触发修订。实验结束后修正了该分支并增加回归测试；已执行的源码快照和协议保持原样，没有按新代码重算成绩。本轮实际学习证据分别包含改写后失败和步骤耗尽，因此并未依赖该漏洞。

## 费用与边界

- 严格试验：0.03581595 USD。
- 引用失败尝试：0.00167730 USD。
- 有效修订试验：0.12978885 USD。
- EvoCoder 所有轮累计：0.19107150 USD，预留 0.00000000 USD，上限 1 USD。

费用按 API usage 与配置单价估算，不等同供应商账单。小样本、共享缓存概念、一次配对运行与重复探针都限制结论；没有四组完整实验或统计显著性证明。候选修订本身已经发生，但本轮没有验证出开发集收益。TTL 重复探针中修订版成功而旧版耗尽预算，无技能也成功，不能把这一单次现象归因为自进化。

工程回归：52 项测试通过；Ruff 与 diff 检查通过。所有 315 个 EvoCoder 请求均已结算，没有 RUNNING 任务或残留执行容器。工程检查不调用付费模型。

## 下一步实验建议

暂不继续在这两道探针上筛选版本。下一轮应冻结新的任务和协议，优先接入真实仓库中可复现的缺陷与回归测试；针对当前异步预算耗尽先分析轨迹、评估技能是否过长或范围过宽。之后用相同预算比较无记忆、轨迹检索、冻结技能与持续修订，并做配对重复运行。任何人工调整都另记为工程改动，不能冒充模型自主改进。
