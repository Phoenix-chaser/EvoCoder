# EvoCoder 真实仓库缺陷修复探索实验

真实仓库接入已验证；模型产生 2 个技能候选，其中 1 个为父版本修订，0 个通过门禁。当前 active：无技能基线。 最终测试：no_skill 1/2，first_frozen_skill 0/2，latest_skill 0/2。未验证出稳定技能收益。

任务为 3 个真实 Python 仓库中的 6 个历史 PR 缺陷重建。python-dotenv 用于 learn，Click 用于 dev，packaging 用于 test，仓库不跨分区。问题摘要与公开复现由开发者整理，参考源码和上游回归测试仅控制端可见。不是 SWE-bench 官方实验，不保证无预训练污染。

## 工程与复现

最终离线验证：6/6。每例均检查原始缺陷公开测试失败、原始缺陷上游回归失败、参考补丁两项均通过。首次环境验证有 2 例失败，原因是相对子进程导入路径；更改为绝对路径后另建 bundle，没有删除测试或覆盖失败记录，没有为此调用模型。

随后第一次付费探索（realworld-002）发现搜索工具漏掉超过 100KB 的源码；模型也出现动作缺少 reason、edit 字段无效等合同错误。该轮中途停止，没有运行 test，费用和原始记录保留在 tool-pilot.json。修复搜索范围/大小限制并明确工具字段后另行冻结协议，缺少非执行性 reason 字段时显式标记模型未提供，不伪造解释；非法 edit 仍拒绝。本轮 dev 是重复使用的开发材料，test 此前未由模型执行；工具改进不能算技能自进化。

源码工具新增按行读取和精确替换，支持大模块中的小改动。执行镜像按 ID 冻结；无网络、非 root 容器执行，finish 后独立验证冻结公开测试和上游回归。

## 学习与技能

[
  [
    "dotenv-empty-cache",
    "COMPLETE",
    "bbf3a46ba6decf41fab5f745176051474c63672d519ef0ea659d5ba4fe411955",
    false
  ],
  [
    "dotenv-backslash-roundtrip",
    "EXHAUSTED",
    "dde192eca5172b2a8a6a9ba2882b814318c93b83ada75468ca03c13c65efd979",
    false
  ]
]

技能原文、父版本与证据见 skills.json；提案只读 learn 公开反馈，dev/test 分数不进入提案。人工工具和环境改动不计作自进化。

## 开发集配对

[
  [
    "bbf3a46ba6de",
    "click-optional-choice",
    true,
    true
  ],
  [
    "bbf3a46ba6de",
    "click-envvar-hints",
    false,
    true
  ],
  [
    "dde192eca517",
    "click-optional-choice",
    true,
    true
  ],
  [
    "dde192eca517",
    "click-envvar-hints",
    true,
    true
  ]
]

每行依次为候选、任务、基线通过、候选通过。门禁保持两个分组改善、零回归、无运行错误。

bbf3a46ba6de：基线 1/2，候选 2/2，改善分组 1；dde192eca517：基线 2/2，候选 2/2，改善分组 0。支持集未通过的候选任务：['dotenv-backslash-roundtrip']。这些是单次探索结果，不能写成稳定提升。

## 冻结最终测试

[
  [
    "packaging-spaced-arbitrary",
    "no_skill",
    "COMPLETE",
    true,
    false
  ],
  [
    "packaging-spaced-arbitrary",
    "first_frozen_skill",
    "ERROR",
    false,
    false
  ],
  [
    "packaging-spaced-arbitrary",
    "latest_skill",
    "EXHAUSTED",
    false,
    false
  ],
  [
    "packaging-set-marker",
    "no_skill",
    "FAILED",
    false,
    false
  ],
  [
    "packaging-set-marker",
    "first_frozen_skill",
    "FAILED",
    false,
    false
  ],
  [
    "packaging-set-marker",
    "latest_skill",
    "FAILED",
    false,
    false
  ]
]

每行依次为任务、方案、状态、通过、是否复用了同版本运行。alias 不是独立重复。最终测试无经验写回。没有完整轨迹 RAG 对照或多次采样，不能由两题得出泛化结论。

最终汇总：no_skill 1/2，first_frozen_skill 0/2，latest_skill 0/2。失败明细：

packaging-spaced-arbitrary / first_frozen_skill：ERROR，ValidationError: 1 validation error for Action
line_count
  Input should be less than or equal to 200 [type=less_than_equal, input_value=240, input_type=int]
    For further information visit https://errors.pydantic.dev/2.14/v/less_than_equal

packaging-spaced-arbitrary / latest_skill：EXHAUSTED，预算内未提交或未通过完整独立验证

packaging-set-marker / no_skill：FAILED，预算内未提交或未通过完整独立验证

packaging-set-marker / first_frozen_skill：FAILED，预算内未提交或未通过完整独立验证

packaging-set-marker / latest_skill：FAILED，预算内未提交或未通过完整独立验证

整体结果尚不足以证明稳定能力提升，也不能由单次差异证明失败由技能导致。

## 费用和交付

本轮估算 0.26626215 USD；EvoCoder 累计 0.52846350 USD；预留 0.00000000 USD。含前次工具探索的真实仓库工作合计 0.33739200 USD。保持同一个 1 USD 累计账本，费用按 usage 和配置单价估算。

完整轨迹在 traces/，本地补丁在 patches/；没有向上游创建 PR 或发送消息。当前入口是冻结源码缺陷修复，自动 CI 触发与依赖迁移尚未接入。

下一步优先处理无关技能的选择/跳过，以及预算内可恢复的动作参数错误反馈；不能靠放宽评分或删除失败任务美化结果。增加任务和重复采样前保留本轮 test，其后再使用时需标为已见开发材料。

工程测试：

Unit tests (no paid model requests):
................................................                         [100%]
48 passed, 10 deselected in 0.21s

Docker integration tests (scripted model):
..........                                                               [100%]
10 passed, 48 deselected in 57.23s

