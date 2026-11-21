# 导入实际代码仓库

`pack` 将本地已准备的任务保存成包含所有文本文件的 JSON 快照。执行器每次由该快照重建环境，避免多个实验互相污染。仓库、问题描述和测试材料应有使用授权；首版只处理小型文本仓库，单快照总计不超过 2MB，导出源码单文件不超过 500KB。

固定上游提交的公开缺陷任务可用 `realworld-prepare` 导入，完整流程见 [真实仓库入口](realworld.md)。

任务合同包含：

| 字段 | 内容 |
|---|---|
| id | 不可重复的任务标识 |
| repository | 仓库名称，用于溯源 |
| source_group | 同源 issue/PR、近重复补丁共享的分组 |
| split | learn / dev / test |
| issue | 给求解 Agent 的公开问题 |
| files | 求解时可见的原始文件映射 |
| editable | 允许导出的完整源码文件列表 |
| public_command | 求解时可运行的测试 argv |
| grader_files | 仅评分容器接收的测试文件 |
| grader_command | 可信控制端指定的评分 argv |

`grader_files` 不能与公开文件同名。评分器不会把 Agent 修改的公开测试拿来替换原始评分资产。学习器不读取 grader 内容。

例如准备以下目录（示意路径，请替换成实际路径）：

```text
work/task-a/repository/       # 缺陷版本，包含公开测试
work/task-a/issue.txt
work/task-a/grader/           # 仓库目录外，包含独立测试
```

```bash
uv run --locked evocoder pack \
  --repo work/task-a/repository --issue work/task-a/issue.txt \
  --editable src/example.py src/helpers.py \
  --public-command '["python","-m","unittest","discover","-s","tests"]' \
  --grader-dir work/task-a/grader \
  --grader-command '["python","grade_tests.py"]' \
  --id task-a --repository team/project-a --source-group project-a-issue-123 \
  --split learn --output experiments/tasks/task-a.json
```

依赖必须事先放入任务镜像。默认 Python 镜像只适合标准库案例；如果使用 pytest 或项目依赖，请准备自己的镜像并设置 `EVOCODER_IMAGE`。镜像需要 Python，且工作区必须适合非 root 运行。核心版故意关闭求解时联网，不在任务执行中下载依赖。

自建清单格式：

```json
{
  "schema": 1,
  "kind": "local-repository-tasks",
  "tasks": [
    {
      "path": "task-a.json",
      "hash": "任务完整 JSON 内容的规范化 SHA256"
    }
  ]
}
```

生成每条 hash：

```python
from pathlib import Path
from evocoder.contracts import Task, digest
task = Task.model_validate_json(Path("experiments/tasks/task-a.json").read_text())
print(digest(task.model_dump()))
```

构造清单后保存版本，禁止根据实验成绩删除困难任务。程序检查任务哈希和跨 split 分组；近重复识别与数据质量仍需要整理者审查。

Mock 只支持带 `offline contract fixture` 标记的内置任务。真实任务需要 live 模型或后续接入本地兼容推理服务。不要给真实任务加该标记绕过限制，Mock 不会因此获得通用修复能力。
