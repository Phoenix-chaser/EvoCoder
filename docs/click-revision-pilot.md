# Click 技能修订小实验

这轮围绕 CLI 帮助文本的显示问题。题目按用户可见症状选择，固定目录中的 8 个上游提交和问题描述在模型调用前冻结。每个任务的原始与参考实现由 Docker 独立验证。训练阶段为历史缺陷的重新执行，模型收到症状、源码文件范围和旧测试命令，控制端保留提交标识、评测分组和上游回归。旧公开测试模块的选择仍提供部分定位线索。

| 分区 | 案例 | 用途 |
| --- | --- | --- |
| 学习 3 题 | 空用法占位符、空字符串默认值、隐藏 Choice 选项时的用法文本 | 第一题只用于形成共享初始技能和支持集回放，后两题用于产生新的执行反馈。三题在以往研究中均为学习材料，本轮重新执行。 |
| 开发 2 题 | 用法中 Choice 括号重复、帮助文本与错误提示中的环境变量 | 两题在旧基准中已经使用过，转作开发诊断，门禁要求在各次配对中都通过。 |
| 留出 3 题 | 帮助段落换行宽度、短帮助中的格式标记、自定义帮助选项的执行顺序 | 第三题是外观相似的反例，重点观察路由是否区分文本显示与执行控制。 |

四组分别使用无经验、历史轨迹检索、冻结技能、允许一次修订的技能。C、D 不把种子任务再次用作修订触发。两组终评须有经过评测的同族版本对照，满足后每组每题运行两次。包括初始门禁、修订门禁和学习支持集在内，最多计划 55 次求解运行和 2 次提案调用。样本用于诊断路由采用、触发、发布和复用的过程，只解释本轮具体问题。

本机有上游 Click Git 镜像时，可重建题目并校验原始缺陷与参考实现。

```bash
make install
uv run --locked python scripts/prepare_click_pilot.py \
  --repo work/benchmark-repos/pallets--click --output experiments/click-pilot-bundle-001
uv run --locked python scripts/verify_frozen_benchmark.py --root experiments/click-pilot-bundle-001
```

复现协议时通过 `comparison-prepare --pilot-seed pallets-click-42ee8a1f60 --repeats 2` 冻结小实验。需使用已验证的数据包和完整费用账本。使用同一智谱账户且账本留有 GLM 第四轮的未确认请求时，可通过 `--carry-reservations-from experiments/comparison-glm-004` 附上中断证据。

发布前分别核对开发集重复胜例、全库退化、基础设施错误。候选未达条件时，报告保留真实轨迹和门禁原因。终评若启动，则报告两组实际版本、合法跳过和无效路由，同时保留全体三道留出题的成绩。[冻结题目](../benchmarks/comparison/click-pilot.json)

## 首次真实运行记录

`comparison-glm-click-001` 使用 GLM-5.3。8 例离线验证通过，3 道留出题的 ID 在运行前核对的 47 份本地 SQLite 状态库中均无历史执行或终评暴露记录。

共同学习得到 2 例失败、1 例步骤耗尽。模型生成了 1 个初始技能。种子门禁已记录 7/8 个开发集求解运行和 2 个支持集求解运行，其中 1 个开发集运行通过。另一个支持集运行收到 HTTP 429，控制器中止门禁。按重复改善要求，即使最后一个开发运行通过，已有成绩也不足以达到两个分组各两次改善。此次保留的是不完整门禁记录，没有签发通过收据。C/D 修订学习与 24 个计划终评格均未启动。

本轮新增已结算估算 360.45692 积分，HTTP 429 请求保留 28.46244 积分待确认。与此前三个待核销请求合计预留 155.08623 积分。该用量为本地估算，后续协议可核对两个原实验目录以沿用同一账本。

## 父子诊断

初始技能可以保留为学习候选。诊断将它从已停止的实验复制到隔离状态，锁定原模型、镜像、任务清单和账本，并按原顺序各运行一次非种子学习题。路由记录用于区分实际应用、跳过和无效决策。只有真实应用父技能且观察到符合学习规则的失败，才会从种子和合格轨迹提出一次修订。

如果得到子版本，开发阶段直接配对比较父版和子版，每题重复两次，并回放它们的学习支持题。诊断结果只用于分析行为变化和退化，生成的报告没有发布收据。活动技能库和留出题仍按各自的正式协议处理。

```bash
uv run --locked evocoder --env-file .env.glm revision-diagnostic-prepare \
  --previous experiments/comparison-glm-click-001 \
  --output experiments/glm-click-diagnostic-001
uv run --locked evocoder --env-file .env.glm revision-diagnostic-run \
  --output experiments/glm-click-diagnostic-001
```

诊断固定单并发，最多 16 次求解和 1 次修订提案。有新的不确定响应时，账本保留费用预留，报告保留当时的阶段和已记录运行。

`glm-click-diagnostic-001` 按所冻结的两个学习题完成。空字符串默认值一题返回 `skill_index=-1`，但模型省略当时合同要求的 `decision`，记录为 `skip_invalid`。求解通过公开测试，未通过独立评分。Choice 占位符一题明确跳过父技能，求解耗尽 16 步。两题实际应用次数均为零，因此没有符合条件的父技能反馈。诊断提案数、父子开发回放数和留出调用数均为零。新增已结算估算 60.48853 积分，预留仍为 155.08623 积分。

模型接口随后简化为单一整数选择。非负编号选择输入技能，`-1` 表示不适用，`-2` 表示依据不足。运行器从编号生成内部的决策和技能 ID，应用前依然校验引用原文、适用条件和排除规则。上述缺字段的历史返回已作为离线回归输入，旧运行结果按冻结合同保留。

`glm-click-routing-v3-001` 单独记录了两次真实路由调用。默认值题按预期跳过，原种子题按预期选中父技能。验证只覆盖路由输出与运行时绑定。两次调用的新增已结算估算为 1.34350 积分，待确认预留没有增加。

## 帮助与用法空白专项诊断

保留相同的父技能和种子快照。三道后续学习题分别是空帮助段多出空行（[Click #2369](https://github.com/pallets/click/pull/2369)）、空描述时弃用标签左侧多出一个空格（[Click #3509](https://github.com/pallets/click/pull/3509)）、选项说明折行后多出空行（[Click #1832](https://github.com/pallets/click/pull/1832)）。前两题共享帮助文本相关源码分组，留在同一个分区。三个缺陷都在原始代码上通过可见断言复现。独立评分保留上游回归测试。#1832 是上游回退，额外编写了独立的宽度和行布局测试，移除了上游撤回的两条旧公开断言。

开发题为无参数时用法行有多余字符（[Click #3434](https://github.com/pallets/click/pull/3434)）和缩进段落过早换行（[Click dcd991d](https://github.com/pallets/click/commit/dcd991d986d43c4f12cc838c4008dfafc015c21d)）。#3434 在历史实验有终评暴露，转作开发诊断。留出题是短帮助泄漏格式标记（[Click 244d914](https://github.com/pallets/click/commit/244d914364e447175549dab8c2414cdb73d86a3c)）与帮助文本在连字符内断行（[Click #3902](https://github.com/pallets/click/pull/3902)）。两道留出题用于观察适用边界及迁移，启动前检查本地历史运行和终评登记。公开上游题存在模型预训练熟悉度的可能。

每道学习题按常规语义路由运行一次。若出现实际应用父版后的合格反馈，控制器只允许生成一个候选。父子各在两组开发题上重复两次，并各回放一次支持题。门禁要求两组开发题的两次对照都改善，改善伴随实际技能行为变化，且开发和支持回放均无退化。只有门禁通过才允许进入留出集。留出只执行同一对版本，每题两次，每次交换调用顺序，不进入后续提案。所有阶段使用同一模型和步骤预算，发布仍由独立流程处理。

```bash
uv run --locked python scripts/prepare_click_spacing.py \
  --repo work/benchmark-repos/pallets--click \
  --seed-bundle experiments/click-pilot-bundle-001 --output experiments/click-spacing-bundle-001
uv run --locked python scripts/verify_frozen_benchmark.py --root experiments/click-spacing-bundle-001
uv run --locked evocoder --env-file .env.glm revision-diagnostic-prepare \
  --previous experiments/comparison-glm-click-001 \
  --manifest experiments/click-spacing-bundle-001/manifest.json \
  --verification experiments/click-spacing-bundle-001/verification.json \
  --output experiments/glm-click-spacing-001
uv run --locked evocoder --env-file .env.glm revision-diagnostic-run --output experiments/glm-click-spacing-001
```

### GLM-5.3 运行结果

冻结目录为 `experiments/glm-click-spacing-001`。8 个快照的原始源码和上游参考源码验证通过。7 道新任务的原始源码均有公开断言失败，留出登记预检扫描了 52 个本地状态库。正式运行使用相同镜像、每题 16 步和每条命令 20 秒的限制。

| 学习题 | 实际路由 | 执行反馈 | 独立评分 |
| --- | --- | --- | --- |
| #2369 空帮助段 | 应用父版 | 早期补丁移除了普通帮助文本中的既有空行，触发公开回归失败。后续补丁恢复了该行为 | 通过 |
| #3509 弃用标签 | 应用父版 | 公开复现和回归通过。补丁修复了命令的帮助渲染，遗漏了独立检查覆盖的选项路径 | 失败 |
| #1832 折行选项 | 应用父版 | 改动后公开测试通过 | 通过 |

前两条轨迹触发一次修订提案，得到父版 `325c34af9897` 的直接子版 `e7169865ac78`。子版扩大了空片段和空行处理的覆盖范围，要求检查相邻渲染情形，并加强全套测试及警告检查。提案引用了可见的测试警告。模型实际提交的补丁显示，#3509 的具体漏修是选项渲染路径。

| 配对集 | 父版通过 | 子版通过 | 改善对 | 退化对 |
| --- | ---: | ---: | ---: | ---: |
| 开发 2 题各 2 次 | 4/4 | 2/4 | 0 | 2 |
| 支持回放 3 题各 1 次 | 2/3 | 3/3 | 1 | 0 |

两次退化来自 #3434。两次子版都通过指定的公开测试，随后调用整个 `public_checks` 目录并超过 20 秒，最终未提交补丁。随后用同一镜像和限额离线复跑。指定测试在原版上失败、在上游参考版上通过。扩大到整个目录的命令在两个版本上都超时。支持集唯一改善发生在原种子上，另两组父子都通过。门禁重复改善组为 0/2，子版保留为候选。两道留出题的 API 调用和实验登记均为零。

本轮新增已结算估算 449.59376 套餐积分，其中父子配对分别使用 175.13508 和 194.93710 积分。总账累计 11234.22649 积分，四条历史预留合计 155.08623 积分。本地 153 项离线工程测试和 3 项 Docker 集成测试通过。

## 控制测试范围后的第二轮修订

第二轮从同一个父技能生成另一个直接子版。控制器核对种子及三次学习运行的任务哈希、模型配置和实际路由，再把种子及两条合格的学习轨迹交给 GLM。工程人员根据上一轮超时现象调整了提案提示，要求优先运行任务指定的 `test` 命令，再用小型断言检查题目点名的其他输出位置。模型据此修订适用条件和操作步骤。两个候选共享父版和学习证据，求解器、预算和独立评分器也保持一致。

| 用途 | 案例 | 规则 |
| --- | --- | --- |
| 已知回归 | 空参数用法输出，Click #3434 | 两次父子配对。退化阻断门禁，改善不计入门禁 |
| 新增开发 | 多行选项帮助的源码缩进（[fd401cd](https://github.com/pallets/click/commit/fd401cd761a2cf15fbb9a54404860b636a5b558d)）、计数选项多出的占位符（[bb7b2df](https://github.com/pallets/click/commit/bb7b2df4d67bcd02a044d21c5ee3c7acb51f5d88)）、用法折行时的前缀缩进（[1a63c21](https://github.com/pallets/click/commit/1a63c21678a0896ba440bc0fda6ef58e3574510a)） | 三组各配对两次。门禁仍要求至少两个分组重复改善，且所有开发与支持回放均无退化 |
| 学习支持 | 父版种子和符合子版证据链的学习快照 | 各配对一次，检查已学行为能否保留 |
| 留出 | 保留短帮助标记与连字符折行两题的原哈希 | 门禁通过后才允许各配对两次 |

新题都保存上游原始与修正源码。公开复现与独立评测在原版上均失败、在上游修复版上均通过。前缀缩进题去掉了一条上游更新过的旧公开断言，独立评分仍使用上游参考测试。准备阶段检查了 53 个本地状态库，三道新增开发题均为首次运行。前缀缩进题与已知回归同属用法行格式化分组。这些提交已公开，模型是否在预训练时接触过它们未知。

以下为本轮首次执行时的参数。它需要包含这些历史提交的上游 Click Git 克隆，沿用之前的账本、镜像、16 步和 20 秒命令限制。已有结果可直接用本节末尾的离线命令复核。

```bash
uv run --locked python scripts/prepare_click_bounded.py \
  --repo /path/to/click --previous-bundle experiments/click-spacing-bundle-001 \
  --output experiments/click-bounded-bundle-001
uv run --locked python scripts/verify_frozen_benchmark.py --root experiments/click-bounded-bundle-001
uv run --locked evocoder --env-file .env.glm revision-diagnostic-prepare --bounded-retry \
  --previous experiments/glm-click-spacing-001 \
  --manifest experiments/click-bounded-bundle-001/manifest.json \
  --verification experiments/click-bounded-bundle-001/verification.json \
  --output experiments/glm-click-bounded-001
uv run --locked evocoder --env-file .env.glm revision-diagnostic-run --output experiments/glm-click-bounded-001
```

### 子技能在新题中改变了什么

GLM 从父版 `325c34af9897` 生成了子版 `11711d86ee1d`。子版要求逐个检查工单点名的输出位置，并用很短的检查覆盖空值、缺值和非空值三种情况。提案提示中限制测试范围的规则来自工程人员；适用条件和操作步骤的具体修订来自 GLM。

| 新开发题（每题配对两次） | 父版实际用技能 | 子版实际用技能 | 父版独立测试通过 | 子版独立测试通过 |
| --- | ---: | ---: | ---: | ---: |
| 多段选项帮助的缩进 | 0/2 | 1/2 | 1/2 | 0/2 |
| 计数选项多出的占位符 | 2/2 | 2/2 | 2/2 | 2/2 |
| 自定义用法前缀的折行缩进 | 0/2 | 2/2 | 2/2 | 2/2 |

子版多用了三次技能，其中两次两边都通过，一次两边都失败。多段选项帮助题的首次配对出现父版通过、子版失败，两边均跳过技能。产品门禁将这条用户可见的结果差异计入退化；这组轨迹无法识别技能内容对该差异的因果作用。

已知回归题 #3434 展示了一个可以检查到具体操作的变化。子版第二次运行时通过 `repr` 看到空参数分支输出两个换行符，随后修改 `HelpFormatter.write_usage`，通过公开与独立测试。父版这次也通过。两版在此题上各通过一次。上一轮的旧子版两次都把测试扩展到整个目录并超时，本轮两次子版运行均未触发该超时。

候选摘要中有一处事实错误。它写道学习题 #3509 的已提交补丁同时修复了命令帮助和选项帮助。逐文件比对该补丁与输入源码，实际只改动了 `Command.get_short_help_str` 和 `Command.format_help_text`，`Option` 方法没有变化。这条记录保留了原摘要、补丁哈希、改动方法和独立评分，可用于检查后续提案的证据引用。

### 门禁与离线复核

| 用途 | 配对数 | 父版通过 | 子版通过 | 子版改善 | 子版退化 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 新增开发 | 6 | 5 | 4 | 0 | 1 |
| 已知回归 | 2 | 1 | 1 | 0 | 0 |
| 学习支持 | 3 | 3 | 3 | 0 | 0 |

门禁要求至少两个开发分组在两次配对中都改善，本轮满足的分组数为零。没有基础设施错误。两个留出题的模型运行与终评登记均为零，子版保留为候选。账本记录本轮新增已结算估算为 685.99920 套餐积分，包括父版配对 322.37460 积分、子版配对 355.02744 积分和一次提案 8.59716 积分。原有四条待确认预留合计 155.08623 积分，本轮没有新增待确认预留。

离线复核将 #3434 首次子版提交的补丁取回，核对内容哈希，再与上游参考修复在同一镜像中运行。空参数时，该补丁输出 `'Usage: Program\n\n'`，参考修复输出 `'Usage: Program\n'`。独立测试的退出码分别为 1 和 0。复核使用已经归档的开发与学习轨迹，无需模型密钥。

```bash
uv run --locked python experiments/glm-click-bounded-001/diagnosis/replay.py \
  experiments/glm-click-bounded-001 /tmp/evocoder-click-bounded-check.json
```

完整记录见冻结协议 `experiments/glm-click-bounded-001/protocol.json`、逐题评分 `experiments/glm-click-bounded-001/result.json`和补丁复核 `experiments/glm-click-bounded-001/diagnosis/paired-traces.json`。

## 让每一条修订摘要都能回到补丁和测试

上一轮的错误来自 Click #3509。模型把“公开测试通过”解释成了“命令帮助和选项帮助都已修好”。这一轮把总结历史事实的职责交给控制器。GLM 继续提出新的适用条件、排除条件和操作步骤，并选择支持这些建议的学习运行。父子技能的有序条目保存在各自的内容寻址产物中，可逐项比较。

每个被引用的运行生成一条事实记录。控制器根据原任务快照、最终提交的文件内容和归档 diff 确认修改路径，通过 Python 语法树比较定位变化的方法。文件摘要保留完整前后哈希。测试结论来自同一提交的公开测试与最终评分事件，检查命令、镜像和评分器指纹。只有工作区测试的轨迹会注明尚未提交以及最后一次工作区测试结果。方法出现在差异中，只说明该方法的源码发生变化。模型的操作建议和修复收益由后续配对评测检验。

新的真实和离线 Mock 修订在常规 CLI、学习流与技能库中使用同一校验。CLI 的 `run` 自动保存新学习任务的本地快照，已有学习轨迹可在 `propose` 时用 `--manifest` 指向原快照。直接使用 Python API 可向 `Evolution` 提供 `grounding_tasks`。外发提案只包含可见学习证据和核实后的事实句，评分器指纹仅在本地核对。历史实验显式固定的旧合同仍用于重放。

### Click #3509 的正反例

在前一轮只读归档的离线副本上，把原候选重新交给准入逻辑。旧摘要被拒，技能库条目数保持不变。改为由控制器生成摘要后，新条目通过。对应的补丁事件 41 只修改 `Command.format_help_text` 和 `Command.get_short_help_str`，公开测试事件 42 通过，最终评分事件 43 失败。单独伪造方法名、翻转公开测试结果或翻转最终评分结果也会被拒绝。校验只接受由事实记录逐条渲染的文本，自由表述可以留在操作建议中。

```bash
uv run --locked python scripts/verify_click_summary.py \
  --previous experiments/glm-click-bounded-001 --output /tmp/evocoder-click-summary-check.json
```

### 真实模型对照

本轮首次运行使用下列参数。模型密钥从本机 `.env.glm` 读取，归档的 `grounding-check.json` 由上一节的离线回放生成。

```bash
uv run --locked python scripts/prepare_click_grounded.py \
  --repo /path/to/click --previous-bundle experiments/click-bounded-bundle-001 \
  --output experiments/click-grounded-bundle-001
uv run --locked python scripts/verify_frozen_benchmark.py --root experiments/click-grounded-bundle-001
uv run --locked evocoder --env-file .env.glm revision-diagnostic-prepare --grounded-retry \
  --previous experiments/glm-click-bounded-001 --grounding-check /tmp/evocoder-click-summary-check.json \
  --manifest experiments/click-grounded-bundle-001/manifest.json \
  --verification experiments/click-grounded-bundle-001/verification.json \
  --output experiments/glm-click-grounded-001
uv run --locked evocoder --env-file .env.glm revision-diagnostic-run --output experiments/glm-click-grounded-001
```

本轮复用同一个父技能和已冻结的学习反馈。GLM-5.3 的温度为 1，每次修复限 16 步，单条命令限时 20 秒。两个版本在相同快照、预算和评分器上交替先后顺序。求解器接收完整技能条目，比较包含操作步骤与摘要的共同影响。

| 用途 | 任务 | 配对次数 |
| --- | --- | ---: |
| 新开发 | 嵌套命令用法行漏掉父级参数，计数选项帮助多出范围说明，错误输出中的帮助提示及空行 | 每题 2 次 |
| 已知回归 | 无参数的用法行，多段选项帮助的缩进 | 每题 2 次 |
| 学习支持 | 父技能种子，以及两次提供合格反馈的历史任务 | 每题 1 次 |
| 留出 | 短帮助的格式标记，连字符处的折行 | 仅在门禁通过后每题 2 次 |

三个新开发题均来自 Click 上游，启动前扫描了历史运行与测试登记。准备时在 11 个快照上运行了 44 次 Docker 校验。嵌套命令题在两个版本都排除了一项与当前镜像的 pytest 标准输入不兼容的历史编码测试，保留上游此次修复的两项回归测试。公开上游题的预训练暴露情况未知。任务清单 `experiments/click-grounded-bundle-001/manifest.json` · 离线验证 `experiments/click-grounded-bundle-001/verification.json`

GLM 一次生成子版 `44f5b7395bfd`。摘要回执 `0e0a1ac80c7d` 绑定 3 条学习轨迹，含 #3509。报告中 6 处有序差异可重建父子技能在适用条件、步骤、验证、排除条件和反例五个字段上的全部变化。

| 集合 | 父版通过 | 子版通过 | 子版独有成功 | 父版独有成功 |
| --- | ---: | ---: | ---: | ---: |
| 新开发题，3 题各两次 | 4/6 | 4/6 | 2 | 2 |
| 历史回归题，2 题各两次 | 1/4 | 3/4 | 2 | 0 |
| 学习支持题，3 题各一次 | 2/3 | 3/3 | 1 | 0 |

计数选项题的首次配对中，父版跳过技能并耗尽 16 步，子版应用技能，用 8 步提交一行修复并通过独立评分。第二次父版应用、子版跳过，两边都通过。#557 的两次配对则各胜一次。针对第一次失败的已提交补丁，离线运行公开工单描述中的“关闭帮助选项”情形，子版仍显示 `Try "inspect --help" for help.`，父版和上游参考修复均省略此行。嵌套命令题第一次两边通过，第二次仅父版通过。#557 输出记录 `experiments/glm-click-grounded-001/diagnosis/usage-error.json`

重复两次都改善的新开发分组为 0，两个新开发任务各出现一次退化，门禁拒绝该候选。两道留出题的模型调用和终评登记均为零。已核对全部 13 对、26 条求解轨迹和一次提案。352 次模型请求共计 771.38658 套餐积分，为本地已结算用量估算。本轮没有新增待确认预留。结果展示了可审计的摘要生成机制，以及路由随重复运行发生变化时，单次胜例的局限。冻结协议 `experiments/glm-click-grounded-001/protocol.json` · 逐例结果 `experiments/glm-click-grounded-001/result.json` · 独立核对 `experiments/glm-click-grounded-001/diagnosis/terminal-check.json`
