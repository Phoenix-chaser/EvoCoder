# 一次真实的 Click 技能修订

在仓库根目录运行 `python3 scripts/verify_revision_example.py`。命令只用 Python 标准库，读取同目录的历史样本并校验两个技能版本和评测收据的内容哈希。

| 环节 | 观察到的事实 |
| --- | --- |
| 执行 | DeepSeek 在处理 `Choice(show_choices=False)` 的 metavar 显示时选择了父技能 `2e387779bd19`，执行仍在 16 步处耗尽。期间成功编辑 1 次，编辑后公开检查通过 0 次。 |
| 修订 | 子版本 `9efa67ccddd0` 引用了该运行，新增了检查 `get_metavar` 的定位步骤和一条误用反例。 |
| 回放 | 历史发布门禁将候选库与当时的空发布库对照，开发集改善 0 组，有 1 条退化。核验命令根据逐例收据重算拒绝判定。 |

样本导出自 `comparison-live-004`，包含原样的技能与收据和选出的公开执行记录。当时任务输入携带由上游补丁派生的定位线索。父子关系和真实修订过程可审计，历史收据衡量的是候选能否进入发布库。新任务修复率的变化需要另做父子版本对照。[实验记录](../docs/comparison-experiment-001.md)

## 复演一条真实的 GLM 关闭侧断言

样例保留了 Choice 学习题第 6 步的覆盖反馈、模型写的脚本、当时的补丁和运行摘要。在项目根目录运行 `uv run --locked python scripts/report_switch_study.py replay`，使用本地 Click Git 镜像与固定 Docker 镜像核对三个版本的退出码及触发断言的行号。[可移植样例](click-live-switch.json) · [完整实验](../docs/click-switch-feedback.md)
