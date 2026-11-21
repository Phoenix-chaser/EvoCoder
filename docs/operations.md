# 模型接入与运行说明

## 离线默认值

默认 mode=mock，模型调用费用为零。`make demo` 只允许 Mock。它真实创建容器、执行代码和测试，模型决策与技能提案由脚本驱动。

运行前执行 `make doctor`；默认镜像不存在时，由操作者准备 `python:3.12-slim` 或项目镜像。程序不会自动拉取镜像。

## 兼容 API

复制 `.env.example` 为 `.env`。至少设置：

```dotenv
EVOCODER_MODE=live
EVOCODER_BASE_URL=https://your-provider.example/v1
EVOCODER_API_KEY=replace-locally
EVOCODER_MODEL=your-model
EVOCODER_BUDGET_USD=1
EVOCODER_INPUT_PRICE_PER_MILLION=填写当前输入单价
EVOCODER_OUTPUT_PRICE_PER_MILLION=填写当前输出单价
```

单价单位为 USD / 百万 tokens，必须用该提供商当前价格填数字。地址后自动拼接 `/chat/completions`。需要提供商支持消息、强制函数选择、工具参数 JSON 和 usage 返回。支持模型超时、输出 tokens 上限和输入字节上限；配置不完整直接报错，不回退离线模型。

`EVOCODER_DISABLE_THINKING=true` 时发送 `thinking: {type: disabled}`，仅供支持该扩展的提供商使用；其他提供商保留 false。

当前默认 `EVOCODER_SKILL_ROUTING=semantic`、`EVOCODER_RECOVER_ACTIONS=true`。有技能时增加一次结构化 `select_skill` 请求，费用与步骤都纳入预算；参数校验失败可返回给模型在剩余步骤内纠正。CLI 可用 `--skill-routing off --no-recover-actions` 做当前运行器的对照，详见 [说明](routing-recovery.md)。这不会自动重发 HTTP、不切换 Mock、不会执行原本被拒绝的动作。

先使用单任务、独立 live 状态目录验证合同，再运行任务流：

```bash
uv run --locked evocoder --mode live --state experiments/live-01/state doctor
uv run --locked evocoder --mode live --state experiments/live-01/state run \
  experiments/tasks/learn-a.json --no-skill
uv run --locked evocoder --mode live --state experiments/live-01/state budget
```

一次 `stream` 可以产生多次解题和对照调用。只有明确准备好任务、模型和累计费用预算后才运行：

```bash
uv run --locked evocoder --mode live --state experiments/live-01/state stream \
  --manifest experiments/tasks/manifest.json \
  --output experiments/live-01/report --max-proposals 3 --with-final-test
```

上述命令是操作示例。已执行的付费实验见 [首轮](live-experiment-001.md) 和 [后续修订](live-experiment-followup.md)，不要把合成 Mock 成绩当作真实模型结果。

`propose` 和 `stream` 默认使用 `--feedback-policy post_edit_or_budget`，包含改写后测试曾失败、随后修复的经历，以及步骤耗尽。若需复现旧规则，显式传 `--feedback-policy final_public_failure`。每轮先冻结策略与源码，不能在看到成绩后覆盖旧协议。

`scripts/run_cache_pilot.py` 和 `scripts/run_feedback_revision.py` 是本地特定探索实验驱动器，后者绑定已有学习轨迹及父技能 ID，不是新环境通用入口。实际实验目录保留协议、源码快照、数据集、轨迹、失败尝试和费用；重复执行可能再次计费，不能当作恢复已中断请求。

## 费用账本

默认状态目录有独立 SQLite 累计账本。首次调用冻结预算 cap，仅修改配置会拒绝；用户明确授权追加预算后，`budget-grant` 通过幂等授权记录增加上限，保留既有费用和全部调用。四组实验的知识状态绑定同一个外部账本，不能脱离它重新开预算。请求前按输入字节与最大输出 tokens 保守预留，正常响应按 provider usage 和配置单价结算。每个请求只发一次，不自动重试。

解析失败但 usage 有效时仍结算。超时、网络断开、非 200 或 usage 无效时保留预留，避免不确定计费被抹掉；当前没有自动释放接口。发生 token 上界超出预留时封闭账本，停止后续调用。可选缓存单价只在明确、完整的命中用量有效时应用；否则按全部输入未命中估算。历史单价差异可单列保守 allowance，不能伪造调用或改写以前的 usage。费用不等同于供应商账单。

模型密钥只在宿主请求适配器中使用，不发送到执行容器，不写入公开配置或报告。`.env`、SQLite 状态和实验目录均被 Git 忽略。新建目录会产生新账本，不能用它规避已授权总预算。

## 中断与恢复

每步动作、观察、补丁、评分、候选和发布结果均持久化。命令行返回码：0 为成功，1 为任务失败/门禁拒绝，2 为配置或运行错误。失败结果也可用 inspect 查询。

Ctrl-C 会尽力销毁当前容器并记录 INTERRUPTED。正常异常会记录 ERROR；强制杀死控制进程或宿主崩溃可能留下 RUNNING 和带 `evocoder.managed=true` 标签的容器。可先查 `docker ps --filter label=evocoder.managed=true`，确认对应 run 后手动移除。首版不恢复执行到中途步骤，后续任务从新快照开始；不能把重新执行称为断点续跑。

提案或评测中断后，已发生的模型费用和已完成运行仍在库中。重新评测会新建配对运行，避免静默复用部分结果；它可能再次计费。不要清库重置费用。

SQLite 控制面是本机可信边界，没有远程多租户鉴权。首版没有后台 worker、Web 控制台或并发发布服务。
