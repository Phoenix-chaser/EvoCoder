# EvoCoder

EvoCoder 是面向小型 Python 仓库的 **Coding Agent 技能修订与评测系统**。它在隔离工作区里修复缺陷，记录技能实际应用时的成功与失败，再让同一个模型修订适用条件、操作步骤和验证方法。评测器用独立测试检查候选，只有通过门禁的版本才进入发布库。

## 先看一次真实修订

```bash
python3 scripts/verify_revision_example.py
```

这条命令无需 API 密钥、第三方 Python 包或 Docker。它核验一次真实 Click 修复中的父技能、执行反馈、子版本和评测收据。该案例中，模型在步骤耗尽后提出了包括 `get_metavar` 检查在内的修改。回放发现 0 个开发集改善组、1 个退化，更新被拒绝。[案例与证据](examples/README.md)

GLM 的 Click 实验还记录了一个容易误判的结果。子版在三道学习题上都执行了关闭场景，但原始自写检查检出对应回归为 0/3。在同一 Bash 学习题上反馈实际遗漏的开关值后，父、子两条轨迹都写出了能检出回归的断言。正常路由父子各通过 3/4，留出门禁关闭。运行 `python3 scripts/verify_step_example.py` 核对错误历史陈述的准入回归，运行 `python3 scripts/replay_click_example.py` 查看三种检查结果。[实验、完整 Docker 复演与门禁](docs/click-missing-state-study.md)

把覆盖反馈接进正常流程后，GLM-5.3 在两道此前未用过、只公开开启用例的 Click 题上完成了四次求解。四次都在原来的 16 步内编写并运行了能抓到关闭回归的断言，且断言在最终提交的同一源码快照上通过。四次学习路由均跳过技能。仓库附有一份真实断言及补丁，可在本地离线复演。[父子对照、门禁与复验命令](docs/click-switch-feedback.md) · [逐次检查报告（HTML）](docs/reports/click-switch-feedback.html)

项目在 7 个 Python 仓库上离线复现了 80 个历史缺陷，按学习／开发／测试分为 32／16／32。DeepSeek 真实生成 3 个修订候选，均被门禁拒绝。旧评测输入曾带有上游定位线索，正式留出集上的技能修订收益尚待验证。[基准与限制](docs/comparison-protocol.md) · [历史结果](docs/comparison-experiment-001.md) · [GLM 实验](docs/glm-coding-plan.md)

新版技能修订会把摘要里的补丁和测试结论绑定到具体学习运行，并记录父子技能的步骤差异。Click #3509 的回放展示了错误的修改范围如何被拦截。[修订摘要校验与真实父子对照](docs/click-revision-pilot.md#让每一条修订摘要都能回到补丁和测试)

计数选项和 Click #557 的配对实验进一步检查了同一技能为何有时被选中、有时被跳过。每条路由决定、实际注入内容、补丁和独立评分可分别核对。[正常路由与固定路由的对照](docs/click-routing-study.md)

后续实验在独立 Click 学习任务上分别检查功能开启和关闭，并将公开测试通过的源码保存为本轮可回退的检查点。学习失败、GLM 的步骤修订及开发门禁均可追溯。[学习与回退实验](docs/click-procedure-study.md)

## 在本机运行

核心演示使用 Python 3.12、uv 和 Docker。以下命令从仓库根目录执行。

```bash
make install
make test
docker pull python:3.12-slim
make doctor
make demo
```

`make demo` 以脚本化 Mock 模型跑通技能生成、修订、评测、发布和后续使用，每次写入新的 `experiments/demo-*/` 目录。打开生成的 `report.html` 查看证据链。需要 Docker 的回归用 `make integration` 运行。

## 自进化具体体现在哪里

首版进化对象是结构化技能：**适用条件、诊断步骤、验证方法、禁用条件和反例**。技能作为可版本化的指导注入求解模型，基础工具、运行器和评分器固定。

1. Agent 在隔离仓库中读代码、改源码、执行测试，保存实际工具反馈和补丁。
2. 提案器读取 learn 任务的公开执行证据，生成完整技能候选，保存证据哈希和父版本。四组实验还允许使用学习任务独立评分失败的布尔信号。
3. 候选可在后续 learn 沙箱中试用。CLI 默认允许根据父技能应用期间的改写后公开测试失败（含后来修复的错误）或步骤耗尽触发修订；`--feedback-policy final_public_failure` 保留原有的最终公开测试失败规则。
4. 提案器收到父技能、公开执行反馈和相关历史，修订技能字段并引用相应运行。预算耗尽表示流程未完成；失败发生时选用了技能，不等于技能导致了失败。
5. 评测器从同一冻结源码分别运行基线与候选，检查改进和退化；未通过则保留当前版本。
6. 接纳后的版本被固定到后续任务。终评期间不创建或修改技能。

可观察的证据链为：`父技能 → 证据快照 → 字段差异 → 对照运行 → 评测收据 → 发布记录 → 新任务使用版本`。

Mock 中的求解和提案是明确编写的夹具驱动器，任务来自同一合成生成器。Mock 的修复结果与技能变化只验证工程合同，不能用来声称系统自动发现了知识或具备泛化提升。真实模式不注入 Mock 的固定技能内容，不回退到 Mock。

## 自动任务流

```bash
uv run --locked evocoder fixtures --output experiments/fixtures
uv run --locked evocoder --state experiments/stream-01/state stream \
  --manifest experiments/fixtures/manifest.json \
  --output experiments/stream-01/report \
  --max-proposals 3 --with-final-test
```

每轮按清单顺序处理 learn 任务，根据反馈修订候选并回放 dev 和支持集。每次 stream 最多三个提案。发布要求至少两个独立 dev 分组出现与技能行为变化对应的胜例，且没有观察到的回归和基础设施错误。真实模式每道 dev 题重复两次，Mock 工程演示运行一次。[门禁设计](docs/skill-library.md)

dev 分数只用于接纳，不交给提案模型；下一次修订依据后续 learn 执行反馈。`--with-final-test` 最后冻结版本运行 test。使用同一 dev 多次筛选仍有过拟合风险，因此最终任务应独立保留。

## 分步操作

所有需要共享状态的命令使用同一个 `--state`。参数位于子命令之前。

```bash
# 1. 无技能处理初始学习任务，记录输出 run_id
uv run --locked evocoder --state experiments/manual/state run \
  experiments/fixtures/learn-01.json --no-skill

# 2. 从学习运行形成候选，记录 skill_id
uv run --locked evocoder --state experiments/manual/state propose --runs RUN_ID

# 3. 后续学习任务试用候选；失败返回码 1 是任务结果，并不抹去轨迹
uv run --locked evocoder --state experiments/manual/state run \
  experiments/fixtures/learn-02.json --skill V1_ID --candidate

# 4. 根据实际父技能失败修订
uv run --locked evocoder --state experiments/manual/state propose \
  --runs INITIAL_RUN_ID FAILURE_RUN_ID --parent V1_ID

# 5. 独立比较；不通过返回码 1，仍保存报告与收据
uv run --locked evocoder --state experiments/manual/state evaluate V2_ID \
  --manifest experiments/fixtures/manifest.json

# 6. 显式发布；自动 stream 会在门禁通过时执行这一步
uv run --locked evocoder --state experiments/manual/state activate V2_ID \
  --receipt EVALUATION_RECEIPT --expected none

# 7. 后续任务默认使用接纳版本
uv run --locked evocoder --state experiments/manual/state run experiments/fixtures/test-01.json
uv run --locked evocoder --state experiments/manual/state inspect RUN_ID
uv run --locked evocoder --state experiments/manual/state skills

# 回退到无技能基线；expected 防止覆盖其他操作刚发布的版本
uv run --locked evocoder --state experiments/manual/state rollback --to none --expected V2_ID
```

发布采用比较后交换。旧评测不能在当前版本已改变时直接发布新候选；回滚到历史技能需要该技能的通过收据。已开始的运行固定其技能版本。

## 接真实任务与真实模型

先阅读 [操作说明](docs/operations.md) 和 [任务格式](docs/task-format.md)。将 `.env.example` 复制为 `.env`，填写兼容 Chat Completions 的 URL、密钥、模型名、价格与独立累计预算，再显式使用 `--mode live`。模型凭据和费用账本保存在本地。

真实任务由 `pack` 导入本地仓库快照，或由 `realworld-prepare` 导入固定上游提交；使用预装项目依赖的镜像。运行时网络关闭，不在线安装包。已完成三个真实仓库的小规模探索，四组完整基线、多个任务顺序与统计报告仍未完成。

## 代码位置

| 模块 | 职责 |
|---|---|
| `contracts.py` | 冻结任务、模型动作、技能与配置合同 |
| `sandbox.py` | 容器隔离、命令超时、有限输出与源码导出 |
| `runtime.py` | Agent 工具循环、完整轨迹、独立补丁评分 |
| `evolution.py` | 冻结证据、反馈修订、对照门禁、版本差异 |
| `stream.py` | 有提案上限的跨任务持续修订循环 |
| `store.py` | SQLite 轨迹/不可变产物、发布记录、累计费用预留 |
| `model.py` | 同合同 Mock 和兼容 API 模型适配 |
| `dataset.py` | 合成工程任务、快照导入、任务哈希与分组隔离 |

详细边界和后续研究协议见 [设计说明](docs/design.md)。当前实验状态见 [验证记录](docs/verification.md)。
