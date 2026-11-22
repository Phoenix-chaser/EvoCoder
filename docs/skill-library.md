# 多技能持续修订

原来的 `stream` 始终围绕一个全局技能修订。新命令 `library-stream` 增加多个技能分支：一类任务形成根技能，之后实际应用该技能的公开失败可以产生子版本；不相关任务明确跳过已有技能后，可以形成另一根技能。模型权重不变，演化对象仍是带证据的适用条件、操作步骤和验证方法。

## 学习与发布

- 学习流默认用同一分支的新版本替换旧版。评测时可用 `--retain-parent` 保留直接父版并加入子版。库最多容纳八个技能，每个同源组合最多包含一个直接父子对。
- 一次语义选择最多注入一个技能。GLM 看到候选的适用条件、禁用条件和从零开始的索引。默认只给题面。设置 `public_shape` 可附加公开测试的文件名、调用和断言形状，路由与求解仍各用原来的动作预算。可选的 `public_preview` 还会给出公开说明和可编辑源码的有限摘录，供 GLM 引用。执行器校验引用和条件索引，再绑定实际技能 ID。发布清单固定路由使用的输入范围。[并列技能路由实验](parallel-routing-study.md)
- 选择也占用原有模型步骤。跳过、条件非法和不确定均不注入技能；只有明确 skip 或空池才能创建新分支。不确定/非法响应不能成为“发现新技能”的依据。
- 修订的父版本必须等于该任务实际应用的版本，并有符合策略的公开执行失败。新分支只冻结当前学习轨迹，避免把不相关分支的失败归到父技能。子版本沿用本分支证据，最多十二个运行；完整祖先证据继续保存在本地。
- 待验证或被拒绝版本只在 learn 沙箱试用；普通任务只能显式使用已发布技能库。单技能发布与技能库发布使用不同的收据和状态，避免隐式互相替换。

## 整体门禁

提案可以新增分支、替换后代或保留直接父版作为并列选项。评测比较当前已发布库和更新后的完整库，两组从相同源码开始，轮换先后顺序，使用同样模型、工具、总步骤和独立评分器。

支持集必须覆盖新旧库所有分支及祖先的学习证据，并且任务快照哈希一致。缺失回放、同一任务 ID 对应不同快照、开发组与学习证据重叠，都会在模型调用前拒绝。自动流程只使用已经产生证据的学习案例，不提前回放尚未处理的学习题。

新收据记录两侧实际应用的版本和路由决定。发布要求至少两个独立 dev 分组中的任务，在各次配对中都出现候选通过、基线失败，且候选实际应用了新版本，或明确跳过了基线曾应用的被替换版本。真实模式每道 dev 题跑两次并交换比较顺序，支持集和 Mock 工程演示各跑一次。报告仍保留全部胜例，双方均未应用技能的胜例单列。任一观察到的 dev／支持集退化或基础设施错误都会阻止发布。

遇到基础设施错误时停止门禁，保留已派发的轨迹。通过后发布完整库的内容哈希，使用比较交换检查版本，并可回滚到以前发布过的库或空库。历史收据保持原规则和原哈希。

这是保守的小样本原型门禁，不是统计显著性或生产质量保证。复用同一开发集筛选仍会过拟合。一个待验证分支没有通过时，后续不相关新分支不会把它一并带入发布库。

终评只使用冻结的已发布库，既不试用学习池，也不形成或修订技能。终评运行标记为 evaluate，不能进入经验提取接口。

## 执行

显式的零费用工程演示不读取 `.env`，所有路由、提案和修复策略均为脚本化夹具，代码仍在真实 Docker 中运行：

```bash
uv run --locked python scripts/run_library_demo.py --output experiments/library-demo-001
```

演示使用两类合成任务：可选文本归一化、可选整数解析。检查新分支、后续失败引发修订、全库回归、发布和终评版本绑定。它不能证明模型自己发现经验或能力提升。普通 Mock 不自动为自然问题生成多技能路由，需显式运行本演示或配置真实模型。

真实任务模式沿用原来的 `.env`、账本和总预算，不需要重建状态：

```bash
uv run --locked evocoder --skill-routing semantic library-stream \
  --manifest PATH/manifest.json --output experiments/library-live-NEW \
  --max-proposals 3 --with-final-test

uv run --locked evocoder library
uv run --locked evocoder run TASK.json --library active

# 单独比较一个提案与现有完整库
uv run --locked evocoder library-evaluate CANDIDATE_ID --manifest PATH/manifest.json
# 让模型在父版、子版和跳过之间选择
uv run --locked evocoder library-evaluate CANDIDATE_ID --retain-parent --manifest PATH/manifest.json
# 真实模式下五轮开发、三轮学习支持，按相同求解请求区分方差
EVOCODER_LIBRARY_ROUTE_CONTEXT=public_shape uv run --locked evocoder --mode live library-evaluate CANDIDATE_ID --retain-parent --repeat-gate --manifest PATH/manifest.json
uv run --locked evocoder library-activate LIBRARY_ID --receipt RECEIPT_ID --expected none
uv run --locked evocoder library-rollback --to PREVIOUS_LIBRARY_ID --expected CURRENT_LIBRARY_ID
```

`--initial-skills ID...` 可显式导入历史提案到学习池；清单必须包含它们的完整学习支持快照。不能只传一个技能 ID 而缺少它的证据。原来的单技能 `run`、`stream`、`activate` 行为保留；新库必须用 `--library` 显式选择，不会悄悄改变已有运行入口。

通用 `--repeat-gate` 对当前部署和候选库配对，开发题五次、学习支持三次。独立来源至少要有三次由实际选用子版带来的修复胜例。每个任务都检查净退化和原始多数退化。本文的四组研究另外复测父版和只给题面的相同双技能库，并将两个对象来源的改善设成必需条件。[五轮研究的门禁与证据](shape-routing-study.md)

## 输出与尚未解决的问题

输出目录保存冻结协议、逐任务时间线、技能产物、完整修复/评测轨迹和总结。技能库清单、整体门禁收据和发布历史持久化到原 SQLite；增加表，不修改历史技能或实验成绩。

目前路由对整个候选库选取至多一个技能。完整回放随库大小增加成本。项目已用真实 GLM 对配置父版与调用方对象子版做并列选择实验，分别记录独立学习题的漏选和误选、开发题上的补丁质量，以及对旧配置任务的影响。[方法与逐题证据](parallel-routing-study.md)
