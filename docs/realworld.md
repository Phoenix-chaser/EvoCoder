# 真实仓库缺陷修复入口

项目定位为持续维护代码仓库的 Coding Agent。首个实际入口是缺陷修复：接收问题与源码快照，复现、定位、改动、回归验证，生成可审查补丁。技能从公开执行反馈提取和修订，模型权重保持固定。CI 失败和依赖迁移是后续任务类型，目前没有自动接入线上 CI 或创建远程 PR。

## 第一批上游案例

| 分区 | 仓库 | 上游修复 | 缺陷 |
|---|---|---|---|
| learn | python-dotenv | [638](https://github.com/theskumar/python-dotenv/pull/638) | 空配置结果重复解析 |
| learn | python-dotenv | [680](https://github.com/theskumar/python-dotenv/pull/680) | 反斜杠写入读取不能往返，吞掉相邻变量 |
| dev | Click | [3578](https://github.com/pallets/click/pull/3578) | 可选参数的元变量出现双层括号 |
| dev | Click | [3884](https://github.com/pallets/click/pull/3884) | 环境变量列表和空值的提示错误 |
| test | packaging | [1531](https://github.com/pypa/packaging/pull/1531) | 任意相等版本后带空白的约束解析失败 |
| test | packaging | [1507](https://github.com/pypa/packaging/pull/1507) | 集合类型 marker 将字符串当成子串匹配 |

完整 40 位缺陷/修复提交、分区、测试范围和问题摘要记录在 `benchmarks/realworld/cases.json`。三个分区的仓库完全不同；问题摘要和公开复现脚本由开发者整理，评分使用修复提交中的上游测试。它们是人工选取的历史 PR 重建任务，不是 SWE-bench 官方实例，也不保证未出现在模型训练中。上游维护者可能使用过 AI，不能将这些修复宣称为纯人工标签。

## 导入和离线核验

```bash
docker build -f docker/realworld.Dockerfile -t evocoder-realworld:py312-v1 docker
uv run --locked evocoder --mode mock realworld-prepare \
  --output experiments/realworld-003/bundle
uv run --locked evocoder --mode mock realworld-verify \
  --manifest experiments/realworld-003/bundle/manifest.json \
  --image evocoder-realworld:py312-v1 \
  --output experiments/realworld-003/verification.json
```

这两个命令不调用模型。prepare 下载固定提交归档，只读入普通 UTF-8 文件，不在宿主机执行仓库代码或安装钩子，不展开归档路径、不跟随链接。下载失败时只对公开 GET 做至多三次网络重试；付费模型请求仍不自动重试。中途未生成完整 bundle 时，可以复用下载缓存重新准备。

快照包括包内全部普通文本源码、文本测试及选定的项目与许可文件；排除二进制、链接、隐藏文件、变更日志与修复 diff。该范围不是完整 Git checkout，当前支持 2MB 的任务资产。所有包内 Python 源码均允许修改，不通过上游改动文件列表向模型透露定位答案。单文件源码导出上限为 500KB。

控制端另存参考源码和溯源清单，不向求解模型发送。grader 文件只进入独立评分容器。四项核验全部符合才通过：缺陷版本的公开复现失败、缺陷版本的上游回归失败、参考版本的公开测试通过、参考版本的上游回归通过。pytest 的收集错误或配置错误不能当作复现了缺陷。

核验输出、任务包和运行协议禁止覆盖。修复环境或任务材料时保留旧记录并另设版本，不根据模型成绩删除失败任务。新案例应先审查问题摘要不含修复代码、验证断言与接口合同一致，并保留上游许可证。

## 付费探索实验

保持 `.env` 中同一个累计状态目录与预算，不新建账本绕过已授权上限。首次在新的机器运行时需先建立自己的配置。脚本在此目录已经有协议或开始标记时拒绝重复启动，不能当作断点续跑。

```bash
# 验证通过后，冻结源码、模型配置、任务清单和镜像 ID
uv run --locked python scripts/run_realworld_pilot.py
# 显式开始真实调用；必须与冻结时的配置及累计费用一致
uv run --locked python scripts/run_realworld_pilot.py --run
```

脚本最多两次技能提案；父版本必须依据实际后续 learn 反馈修订。门禁仍是两个 dev 源组改善、零观察到的回归、没有运行错误。验证后才能激活。最终测试固定无技能、初次技能、最后技能；相同技能版本复用同一执行并标记 alias，不能当成重复实验。未通过门禁的候选仅在评测中比较，不能伪装成已发布能力。

结果包含原始轨迹、源码快照、费用、技能父子关系和本地 `patches/*.patch`。它不向 GitHub 发消息、不创建或合并远程 PR。生产接入还需要人审补丁及项目自己的 CI；测试通过只说明覆盖到的合同成立。

目前仅为跨仓库小规模探索，缺少原始轨迹 RAG 对照、更多仓库、重复采样和统计分析。即便某个候选获胜，也不能由两道 test 得出普遍泛化结论。

## 真实源码工具

`read` 支持 `start_line` 与 `line_count`，默认显示 120 行，返回原文件行号。`edit` 用 `old_text/new_text` 精确替换单个匹配；零匹配或多匹配都不写文件。write/edit 都受允许修改文件列表约束。只有实际成功的 write/edit 才计入“改写后公开测试失败”，command 中的间接修改仍可能被保守反馈规则漏掉。

`search` 支持文件/目录 path 范围，处理至多 500KB 的源码，80 条匹配后明确提示截断。reason 是诊断字段，模型省略时写入 `[model omitted action rationale]`，不生成替代解释；执行工具、路径、编辑内容和结构错误仍需通过验证。

实际准备过程分别保留了 `realworld-001` 的导入路径失败、`realworld-002` 的搜索限制和模型动作错误；后者未执行最终测试。修正工程问题后使用 `realworld-003` 重新冻结协议。开发案例已经用过，最终案例此前未交由模型执行；所有费用累计，不删除失败尝试。
