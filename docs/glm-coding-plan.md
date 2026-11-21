# GLM-5.3 国内 Coding Plan 接入

本实验使用国内智谱 Coding Plan 的 GLM-5.3。既有 DeepSeek 终评保留 157/384 条记录，两种模型分别评估。

## 配置与权限

从仓库根目录创建 `.env.glm`，设置文件权限为 `0600`，填入套餐专用 `EVOCODER_API_KEY`。仓库提供 `.env.example` 作为配置示例，运行配置保存在本地。

国内端点为 `https://open.bigmodel.cn/api/coding/paas/v4`，模型 `glm-5.3`。[官方接入说明](https://docs.bigmodel.cn/cn/coding-plan/quick-start) 区分套餐密钥与标准 API 密钥。

[官方适用范围](https://docs.bigmodel.cn/cn/coding-plan/tool/others) 将套餐限定于指定工具与产品环境，EvoCoder 不在当前公开列表。接口兼容不等于套餐权限保证；自建工具权限以供应商确认及实际响应为准。接入使用正常 Bearer 调用，不模仿其他工具；套餐配置只接受官方 coding 端点。

## 模型与计量

- [GLM-5.3](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3) 必须启用思考。本次接口验证固定 `reasoning_effort=low`、输出上限 8192、请求超时 180 秒，温度 1；不是与 DeepSeek 的等算力模型排名实验。
- [对话补全文档](https://docs.bigmodel.cn/api-reference/模型-api/对话补全) 列出 `tool_choice=auto`。只接受完整的单一函数调用；无效结果不执行工具，按原有步骤预算反馈。完整 usage 才结算，响应不确定时保留预留，不重发原请求。
- [套餐计量](https://docs.bigmodel.cn/cn/coding-plan/overview) 的 GLM-5.3 输入、缓存、输出系数为 6.9、1.7、24，除以 10000。换算成每百万 token 为 690、170、2400 积分。本地按最高时段估算，不自动采用非高峰折扣。
- 只有 `prompt_tokens_details.cached_tokens` 为合法整数、介于零与输入 token 总数之间时才采用缓存系数；缺失或异常按未缓存计算。供应商实际积分账单为准。
- 每个积分账本使用独立目录，输出单位为 `credits`。美元账本与积分账本分别结算。
- `EVOCODER_UNLIMITED_CREDITS=true` 可关闭本地积分总量上限。运行器继续记录每个请求的预留与结算，并执行每题 16 步和候选次数限制。

## 首次验证

先填好 `.env.glm`，再执行以下命令。目录必须为新目录，不能反复重启已执行的付费请求。

```bash
uv run --locked python scripts/run_glm_pilot.py \
  --env-file .env.glm \
  --output experiments/glm-pilot-001 \
  --manifest experiments/comparison-benchmark-001/manifest.json
```

该入口先验证函数调用与 usage，再在预先指定的 `learn` 案例 `tkem-cachetools-d3598664ca` 上执行真实 Docker 修复和独立公开/隐藏评分，最后从公开轨迹提取一个未发布技能。不给求解器参考补丁，不调用 test 案例，不自动发布技能。

成功只说明接口、代码执行、评分与提案合同打通，不证明自进化收益。原始协议、源码、轨迹、模型标识、技能与积分保留在运行目录。

普通 CLI 可通过 `--env-file .env.glm` 选择独立配置，例如 `budget`、`inspect` 和 `run`；GLM 的最终四组实验需要独立冻结协议。DeepSeek 已暴露部分 test 案例，复用这些案例时必须披露为跨模型复现实验，不能冒称控制器从未见过的新测试集。

## 实际预验证与合同修订

`glm-pilot-001` 的函数探测通过，`tkem-cachetools-d3598664ca` 真实修复运行 `65d9046b82c44b26b7a3e276e5082314` 的公开测试与隐藏回归均通过。随后技能提取返回字符串 `"null"`；增加精确枚举、再改用单一 `null` 类型后，两个独立提案分别返回字符串 `"null"`、`"None"`。三次均被父版本绑定检查拒绝，原始输出、源码和积分未修改。未重发原 HTTP 请求，未重跑已有修复。旧合同三次提案后停止。

新合同 `skill_content_v2` 将模型职责限制为 `SkillContent`：名称、适用条件、步骤、验证、排除条件、反例、证据引用和修改说明。父版本属于运行器不可变元数据，不出现在函数输出 schema；额外提交该字段会被严格拒绝，而非删除、强制转换或当作有效提案。运行器从已验证输入绑定父版本，再由演化模块检查证据、修订内容和谱系；技能产物仍包含父版本且参与哈希。根提案与子版本使用相同机制，Mock 与真实模型产物合同相同。

`glm-content-pilot-001` 从原修复的同一份公开证据单次验证新合同成功，未使用最终测试任务。生成未发布根技能 `1c8a2ad9f1d36d0279275808d4f003951c29f360aebb639d3163e6804e0862d2`。新合同验证约 2.97374 积分，GLM 所有预验证累计约 22.84854 积分、预留为零。旧失败均保留；这是工程合同调整，不是模型自行进化，也不是技能收益证据。

供应商按账号汇总套餐用量，实验按运行记录估算成本。[官方套餐用量查询说明](https://github.com/zai-org/zai-coding-plugins/blob/main/plugins/glm-plan-usage/skills/usage-query-skill/scripts/query-usage.mjs)

## 独立四组实验

```bash
uv run --locked evocoder --env-file .env.glm comparison-prepare \
  --manifest experiments/comparison-benchmark-001/manifest.json \
  --verification experiments/comparison-benchmark-001/verification.json \
  --output experiments/comparison-glm-001 --repeats 3 --workers 4 \
  --replication-from experiments/comparison-live-004
uv run --locked evocoder --env-file .env.glm comparison-run \
  --output experiments/comparison-glm-001
```

新学习与知识状态不继承 DeepSeek 轨迹、初始技能或终评结果。积分费用沿用 GLM 独立累计账本；冻结源码后不在运行中修改。32 道最终测试、四组、三次重复共 384 次求解；仅在完整矩阵且实际应用修订技能后，才按预定 D 对 C 统计标准判断收益。模型别名、推理设置与此前 DeepSeek 不同，不将二者解释成等算力模型排名。

## 路由合同修订与中止记录

`comparison-glm-001` 已在初始技能门禁阶段停止，未执行 C/D 学习或终评。保留 32 例共同学习（17 通过、13 失败、2 步骤耗尽）、3 个模型生成根技能和 90 次门禁求解。前两个根门禁均拒绝，第三个未形成收据。没有发布或技能修订，不能称为自进化收益。

诊断发现旧库路由经常返回非空技能 ID 同时要求 skip/uncertain；原始可见输出包括字符串 `"null"`。计入共同学习克隆的 122 次路由事件中，77 次无技能、43 次无效路由安全跳过、2 次实际应用。43 次无效中，38 次违反跳过时不得选技能的约束，5 次违反响应 schema。所有异常和原结果保留，不把无效决定改成有效的语义跳过。

停止等待已派发回放结束后，1371 次 GLM 调用全部结算，总计约 2683.29180 积分、预留为零；其中本轮约 2660.44326 积分，包含被停止的工程合同验证费用。

新线协议 `integer_library_selection_v2` 要求模型返回决策和整数 `skill_index`：适用时选择输入编号，跳过/不确定时返回整数 -1。schema 不提供或接受 `skill_id`；运行器根据原输入绑定 ID。严格拒绝字符串编号、越界、额外 ID 和决策冲突，不对旧输出做强制转换。技能内部谱系、发布 ID 与哈希合同不变。

真实路由验证 `glm-routing-pilot-001` 前两个场景通过，多技能场景同时返回 apply 和非空排除索引，仍被拒绝。随后在字段 schema 中澄清排除索引只表示已触发的禁止条件，而不是所有检查过的条件；`glm-routing-pilot-002` 单次复核的三个预先声明场景全部通过。两轮分别约 2.07691、2.18929 积分，累计账本约 2687.55800 积分、预留为零。它们只验证合同，不测量路由泛化准确率或自进化收益。

第二版 `comparison-glm-002` 仅复用第一版 32 例**无记忆**公开学习轨迹，不重复已完成的真实修复。无记忆动作循环没有因路由改动而改变。新初始技能与门禁重新生成，不复用旧门禁或测试结果。协议与 `preparation-audit.json` 保留复用哈希、旧轮中止原因和两次路由验证，披露接口澄清使用过先前 learn/dev 诊断；同一 dev 集被重复使用，仍属探索性实验。

## Agent 命令预算与基础设施错误

第二版在第三个根门禁遇到 `pallets-click-4f9086bf8f` 的执行超时，旧运行 `af0f76bdec064972896ecba196d11bd0` 保留为 ERROR。模型将指定的 `public_checks/test_testing.py` 扩大为整个 `public_checks` 目录。离线重建其三个成功编辑后，扩大后的命令在原始代码和模型补丁上均超过 20 秒；原指定命令则均在 1 秒内完成且通过 26 项公开测试。不能据此认定补丁导致超时。

旧运行器将 Agent 命令超时与基础设施错误一并归为 ERROR，导致整个门禁停止；独立公开验证原本已将相同程序超时作为不通过处理。现统一预算语义：只有 Agent 请求的 command/test 在沙箱中超过执行预算，才记录为 EXHAUSTED、`exhaustion_reason=solver_command_timeout`，保存公开的退出码 124 反馈，销毁容器并停止该任务，不补发请求或继续修改。它不是成功提交，评分仍为失败。

API 超时、未知响应、Docker 启动、读写与导出失败仍为 ERROR，继续停止付费实验；不释放不确定费用。执行反馈区分 `step_budget_exhausted` 和 `command_budget_exhausted`，后者只作为流程预算失败，不冒充正确性测试失败。实际应用父技能的 learn 运行才可据此提出修订。

第二版保留 89 次门禁求解、三根技能和两个拒绝收据，第三根没有收据、未发布；2330 次 GLM 请求均结算，账本累计约 4525.49992 积分，预留为零，终评仍为 0。第三版 `comparison-glm-003` 只复用同一 32 例共同学习与这三个未改动根技能，不重放旧错误、不重标旧成绩、不将未完成门禁当作通过。C/D 获得同一初始候选池；新运行统一采用新的命令预算策略，准备审计披露该工程迭代和保守保留未发布状态。

## 第三版保留状态：智谱 HTTP 500 中止

第三版源码冻结为 `c5218ddf67558621ef0cff034a8afba72a2a4b0a45d28f59d99899e8af1c4b80`，C 组保留 12 条新运行：8 通过、2 失败、1 步骤预算耗尽、1 接口错误。最后运行 `7470c7934cee43a8ac751f1c9a654e0c` 在 `mahmoud-boltons-4815fc8dd1` 上收到 HTTP 500，按协议停止，没有重发请求或回退模型。

本轮新增已结算估算 304.98431 积分，另一个缺少完整响应的请求保留了预留。运行器将这次故障记录为 HTTP 500。

D 学习、技能修订和 384 次终评尚未开始，最终矩阵为 0/384。生成的三个根技能不等于持续修订，工程修复也不等于自进化。当前没有 GLM 修订收益结论。报告保留源码、协议、准备审计、学习轨迹、模型输出和 `stop-audit.json`。

后续运行需先核对 C 的学习前缀，保留中断任务及未结算记录，再从新的检查点恢复。

## 第四版结果与反馈优化

`comparison-glm-004` 从 C 第十三个任务继续。C、D 均留下 32 条学习记录。C 实际应用技能 4 次，其中 1 次原规则允许修订，由对照策略抑制。D 实际应用 5 次，原规则产生 0 次修订提案。两组都没有已发布技能。

终评记录 226/384 条，其中 5 条 ERROR。三次来自 Agent 选择了允许范围之外的编辑路径，两次来自智谱 HTTP 500，后者使调度停止。GLM 累计已结算估算为 10362.34378 积分，3 个未确认请求合计预留 126.62379 积分。C、D 实际都以空技能库进入终评，这批成绩无法衡量修订效果。

离线复算 D 的真实学习轨迹时，新策略 `learn_outcome` 识别出 `pallets-click-61f8101f4e` 和 `pallets-click-1a4d8c1bb1`。两次都在应用父技能和编辑后通过独立公开测试，但未通过最终评分。新策略只给学习提案器一个布尔反馈，提案器仍需以公开轨迹为依据，产出可回归的修改。该复算只验证触发条件。

新建四组协议要求 D 发布对 C 冻结技能的修订，且两组技能分支一致，满足后才派发终评。超出允许范围的 write/edit 会被拒绝并反馈，允许纠错时使用原有模型步数继续。以上改动使用旧轨迹和受控测试验证。本轮优化未发起新的模型请求，下一轮收益评估需要新的留出测试任务。
