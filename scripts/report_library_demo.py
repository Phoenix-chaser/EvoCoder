"""Export the zero-provider-cost engineering demo, including raw evidence and limitations."""
import argparse
import html
import json
import shutil
from pathlib import Path

from evocoder.contracts import Settings, canonical, digest
from evocoder.evolution import Evolution
from evocoder.store import Store


def table(headers, rows):
    def cells(values, tag):
        return ''.join(f'<{tag}>{html.escape(str(v))}</{tag}>' for v in values)
    return ('<div class="scroll"><table><tr>' + cells(headers, 'th') + '</tr>' +
            ''.join('<tr>' + cells(row, 'td') + '</tr>' for row in rows) + '</table></div>')


def main(root, output):
    report = json.loads((root / 'result.json').read_text())
    settings = Settings(_env_file=None, mode='mock', state_dir=root / 'state')
    store = Store(settings.state_dir)
    lineage = Evolution(settings, store).lineage()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / 'report', output / 'data', dirs_exist_ok=True)
    shutil.copytree(root / 'bundle', output / 'bundle', dirs_exist_ok=True)
    shutil.copy2('work/library-tests.log', output / 'tests.log')
    shutil.copy2('work/library-unit-tests.log', output / 'unit-tests.log')
    shutil.copy2('work/library-budget-before.json', output / 'paid-ledger-before.json')
    shutil.copy2('docs/skill-library.md', output / 'implementation.md')
    tests = (output / 'tests.log').read_text() + '\n补充单元回归：\n' + (output / 'unit-tests.log').read_text()
    with store.connect() as db:
        releases = [dict(r) for r in db.execute('SELECT * FROM library_releases ORDER BY seq')]
        runs = [store.run(r[0]) for r in db.execute('SELECT id FROM runs ORDER BY created')]
    manifest_ids = {r['after_id'] for r in releases if r['after_id']}
    summary = {'report': report, 'lineage': lineage, 'releases': releases,
               'evidence': {v['evidence_hash']: store.get_artifact(v['evidence_hash']) for v in lineage},
               'manifests': {key: store.get_artifact(key) for key in sorted(manifest_ids)},
               'runs': len(runs), 'model_calls': sum(r['result']['cost']['calls'] for r in runs),
               'test_output': tests, 'source_hash_matches': digest({p.name: p.read_text() for p in
                   sorted((root / 'report/source').glob('*.py'))}) == report['source_hash']}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    def short(key):
        return key[:10] if key else '无'
    rows = [[r['task_id'], short(r['applied_skill']), short(r.get('parent')), short(r.get('candidate')),
             r.get('gate_passed'), r['decision']] for r in report['timeline']]
    final = [[r['task_id'], short(r['skill_id']), r['purpose'], r['state'], r['result']['passed']]
             for r in report['final_runs']]
    text = ('# EvoCoder 多技能持续修订工程验证\n\n'
            '日期：2026-11-17。本次是脚本化夹具与真实 Docker 的工程验证。路由、提案和解题策略均为开发者编写，'
            '没有请求真实模型，不能声称自动发现知识或取得泛化提升。\n\n'
            '## 实际流程\n\n'
            '1. 可选文本任务形成根技能；开发集没有改善，未发布，保留为学习候选。\n'
            '2. 整数任务跳过文本技能，建立独立根技能；通过整体门禁，发布单成员库。\n'
            '3. 文本边界任务实际应用原文本技能并失败；依据公开执行证据产生子版本，'
            '保留已发布的整数分支，通过全库回归后发布双成员库。\n'
            '4. 终评只使用冻结的已发布库；两个任务分别绑定自己的技能，均通过。终评数据不能写入学习证据。\n\n'
            '## 版本与结果\n\n' + canonical(rows) + '\n\n'
            f'完成状态：{report["completed"]}；提案 {report["proposal_count"]} 个；根分支 '
            f'{sum(v["parent"] is None for v in lineage)} 个；发布 {len(releases)} 次；'
            f'真实 Docker 修复/评测运行 {len(runs)} 次；付费请求 0 次。\n\n'
            '全库回归中，即使新技能改善两组，只要旧技能在 dev 或支持集出现退化，也必须拒绝。'
            '受控测试另行覆盖该拒绝路径、快照改变、缺失支持、模式混用、伪造收据、版本冲突和回滚。\n\n'
            '## 自动化测试\n\n```text\n' + tests + '\n```\n\n'
            '原有付费账本未重置；累计估算费用仍为 0.66770520 USD，787 次已结算请求，预留为零。'
            '本轮没有向真实模型请求多技能提案或发布真实模型技能库。\n\n'
            '## 限制\n\n'
            '最多八个分支、单次最多注入一个技能、每轮最多三个提案；没有向量检索、技能组合或自动遗忘。'
            '整体回放会增加费用；模型错选、泛化失误和重复开发集筛选的过拟合仍未解决。'
            '自然仓库上的多技能持续修订收益仍待冻结任务与独立实验验证。\n')
    (output / 'report.md').write_text(text)
    (root / 'report.md').write_text(text)
    (output / 'report.html').write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width,initial-scale=1"><title>EvoCoder 多技能持续修订</title>'
        '<style>body{font:16px/1.7 system-ui;max-width:1120px;margin:36px auto;padding:0 24px;color:#19303c}'
        'th,td{border-bottom:1px solid #d3dee2;text-align:left;padding:10px}table{border-collapse:collapse;'
        'width:100%;font-size:14px}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
        'padding:20px;background:#eff5f7}.note{background:#fff4dc;padding:20px}a{color:#17608c}</style>'
        '<h1>EvoCoder：多个技能分支持续修订</h1><p class="note">工程验证：两个根分支、一个实际失败修订、'
        '整体回归与冻结终评。模型行为为脚本化夹具；不代表真实模型能力提升。</p>'
        '<p><a href="summary.json">完整数据与版本差异</a> · <a href="data/protocol.json">冻结协议</a> · '
        '<a href="implementation.md">实现与运行</a> · <a href="tests.log">测试日志</a> · '
        '<a href="report.md">完整报告</a></p>'
        '<h2>学习过程</h2>' + table(['任务', '实际技能', '父版本', '新提案', '门禁通过', '决定'], rows) +
        '<h2>冻结终评</h2>' + table(['任务', '实际技能', '用途', '状态', '通过'], final) +
        '<h2>证据与边界</h2><pre>' + html.escape(text) + '</pre></html>')
    key = Settings().api_key
    for path in output.rglob('*'):
        if path.is_file() and key and key.encode() in path.read_bytes():
            raise ValueError('credential found in artifact export')
    print({k: summary[k] for k in ['runs', 'model_calls', 'source_hash_matches']})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('experiments/library-001'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    main(args.root, args.output)
