# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib==3.11.2"]
# ///
"""Run 'uv run --script docs/results/plot.py' to redraw the charts. Chinese labels use Noto Sans CJK SC."""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.path import Path as MarkerPath

HERE = Path(__file__).resolve().parent
IMAGES = HERE.parent / "images"
PAPER, INK, MUTED = "#F7FAFB", "#172E39", "#49616C"
TEAL, SLATE, TRACK = "#08796E", "#536C8A", "#DAE4E7"
CHECK = MarkerPath([(-0.58, -0.02), (-0.18, -0.40), (0.57, 0.43)])
TEXT = {
    "zh": {
        "font": "Noto Sans CJK SC",
        "mock": "合成开发任务的修复结果", "pass": "通过数",
        "parent": "父版", "revised": "修订版",
        "key": "实心圆表示通过，空心圆表示未通过",
        "study": "开发任务的配对结果", "trials": "{tasks} 道题 × 每题 {repeats} 次，两版使用相同的模型和步数预算",
        "repair_passed": "修复通过独立测试", "detected_on_submitted_patch": "在提交补丁上\n检出关闭回归",
    },
    "en": {
        "font": "DejaVu Sans",
        "mock": "Repairs on synthetic development tasks", "pass": "Passed",
        "parent": "Parent", "revised": "Revised",
        "key": "Filled circles passed. Open circles failed.",
        "study": "Paired development results", "trials": "{tasks} tasks × {repeats} repeats with the same model and step budget",
        "repair_passed": "Independent repair\ntests passed", "detected_on_submitted_patch": "Off-state regression\ncaught on final patch",
    },
}


def canvas(height, title):
    fig = plt.figure(figsize=(7.1, height), facecolor=PAPER)
    fig.text(0.055, 0.87, title, fontsize=17, weight="bold", color=INK)
    return fig


def mock_chart(data, labels):
    rows = data["development"]
    count = len(rows)
    fig = canvas(2.55, labels["mock"])
    ax = fig.add_axes([0.055, 0.20, 0.89, 0.55])
    ax.set(xlim=(-1.70, count + 0.63), ylim=(-0.50, 1.92))
    ax.set_axis_off()
    for index, row in enumerate(rows):
        ax.text(index, 1.67, row["task"], color=MUTED, ha="center", fontsize=11)
    ax.text(count + 0.16, 1.67, labels["pass"], color=MUTED, ha="center", fontsize=11)
    for y, arm in [(1.0, "parent"), (0.0, "revised")]:
        ax.text(-1.7, y, labels[arm], color=INK, va="center", weight="bold", fontsize=13)
        for index, row in enumerate(rows):
            passed = row[arm]
            color = TEAL if passed else "#A8BAC0"
            ax.scatter(index, y, s=390, facecolor=color if passed else PAPER, edgecolor=color, linewidths=1.35)
            ax.plot(index, y, marker=CHECK if passed else "_", markersize=9.5, markerfacecolor="none",
                    markeredgecolor="white" if passed else MUTED, markeredgewidth=1.85, linestyle="none")
        ax.text(count + 0.16, y, f"{sum(r[arm] for r in rows)}/{count}", color=INK,
                va="center", ha="center", weight="bold", fontsize=16)
    fig.text(0.055, 0.07, labels["key"], color=MUTED, fontsize=10)
    return fig


def live_chart(study, labels):
    dev = study["development"]
    count = dev["paired_trials"]
    fig = canvas(3.06, f'{study["model"].upper()}  ·  {labels["study"]}')
    fig.text(0.055, 0.755, labels["trials"].format(tasks=dev["tasks"], repeats=dev["repeats_per_task"]),
             color=MUTED, fontsize=11)
    ax = fig.add_axes([0.055, 0.08, 0.90, 0.61])
    ax.set(xlim=(-1.08 * count, 1.19 * count), ylim=(-0.42, 3.94))
    ax.set_axis_off()
    ax.hlines(1.76, -1.08 * count, 1.19 * count, color=TRACK, linewidth=0.9)
    for metric, center in [("repair_passed", 2.95), ("detected_on_submitted_patch", 0.55)]:
        ax.text(-1.08 * count, center, labels[metric], color=INK, weight="bold", fontsize=11, va="center")
        for arm, offset, color in [("parent", 0.36, SLATE), ("revised", -0.36, TEAL)]:
            y, passed = center + offset, dev[metric][arm]
            ax.text(-0.055 * count, y, labels[arm], color=color, fontsize=11, va="center", ha="right")
            ax.barh(y, count, height=0.43, color=TRACK)
            ax.barh(y, passed, height=0.43, color=color)
            ax.vlines(range(1, count), y - 0.215, y + 0.215, colors=PAPER, linewidth=1.3)
            ax.text(1.09 * count, y, f"{passed}/{count}", va="center", ha="center",
                    color=INK, fontsize=13, weight="bold")
    return fig


def uptake_chart(receipt, lang):
    zh = lang == "zh"
    fig = canvas(3.08, "同一道题上，工具动作分布改变了多少？" if zh else "How far did the action mix move?")
    fig.text(.055, .76, "每行是一组父子配对，横轴为总变差距离"
             if zh else "Each row compares parent and child. The axis is total variation distance.",
             color=MUTED, fontsize=10)
    ax = fig.add_axes([.28, .19, .65, .48], facecolor=PAPER)
    records = {(p["task_id"], p["repeat"], p["policy"]): 100 * p["tv"] for p in receipt["matrix"]["pairs"]}
    keys = sorted({(task, repeat) for task, repeat, _ in records})
    labels = []
    for i, (task, repeat) in enumerate(keys):
        a, b = (records[task, repeat, policy] for policy in ("fixed_skip", "fixed_apply"))
        ax.plot([a, b], [i, i], color="#ABBDC3", linewidth=2, zorder=1)
        ax.scatter(a, i, s=62, marker="s", color=SLATE, zorder=3)
        ax.scatter(b, i, s=80, marker="o", color=TEAL, zorder=2)
        name = ("计数选项" if zh else "Count option") if task.endswith("3d029e03eb") else "Click #557"
        labels.append(f"{name} · {repeat + 1}")
    ax.set(xlim=(0, 55), ylim=(len(keys) - .5, -.5), yticks=range(len(keys)), yticklabels=labels)
    ax.set_xticks(range(0, 51, 10), [f"{n}%" for n in range(0, 51, 10)])
    ax.tick_params(length=0, colors=MUTED, labelsize=10)
    ax.grid(axis="x", color=TRACK, linewidth=.7)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.text(.06, .045, "■  两边均跳过技能" if zh else "■  Both skills withheld", color=SLATE, fontsize=11)
    fig.text(.48, .045, "●  两边均注入对应技能" if zh else "●  Both skills applied", color=TEAL, fontsize=11)
    return fig


def diversity_chart(study, lang):
    if not study["published"]:
        if study["curve"] or study["holdout"]:
            raise ValueError("unreleased study has holdout observations")
        return None
    points = study["curve"]
    if len(points) != 3 or len(study["holdout"]) != 30:
        raise ValueError("a complete frozen holdout is required for the diversity figure")
    zh = lang == "zh"
    fig = canvas(3.8, "修复收益与任务族多样性" if zh else "Repair gain vs. task-family diversity")
    fig.text(.055,.77,"每个新增任务族 10 个冻结配对" if zh else "Each additional family contributes 10 frozen pairs.",
             color=MUTED,fontsize=10)
    ax = fig.add_axes([.16, .28, .75, .42], facecolor=PAPER)
    ax.axhline(0, color=SLATE, linewidth=.8)
    x = [p["families"] for p in points]
    ax.plot(x, [p["paired_gain"] * 100 for p in points], marker="o", color=TEAL,
            label="等权累计收益" if zh else "Equally weighted cumulative gain")
    ax.scatter(x, [p["family_gain"] * 100 for p in points], marker="x", s=55, color=SLATE,
               label="新增任务族收益" if zh else "Added family's gain")
    ax.set(xticks=[1, 2, 3], xticklabels=["单值","加多层","加批处理"] if zh else ["Scalar","+ Layered","+ Batch"],
           ylabel="修复率差（百分点）" if zh else "Repair gain (pp)")
    ax.legend(loc="upper center",bbox_to_anchor=(.5,-.23),ncol=2,frameon=False,fontsize=9)
    return fig


def pretest_chart(study, lang):
    zh = lang == "zh"
    fig = canvas(3.2, "人工技能在哪些预试题上纠正了假值错误？" if zh else "Which falsy errors did the skill fix?")
    fig.text(.055, .765, "每格为同一题、同一轮的两次独立求解" if zh else "Each cell pairs two independent repairs on the same case and repeat.",
             color=MUTED, fontsize=10)
    ax = fig.add_axes([.055, .19, .92, .51])
    ax.set(xlim=(-1.6,2.53), ylim=(-.50,4.03))
    ax.set_axis_off()
    names = {"transfer-miss-handler":"boltons", "transfer-cardinality":"more-itertools", "transfer-size-hook":"cachetools",
             "transfer-queue-packet":"队列配置往返" if zh else "Queue round trip"}
    cells = {(x["task"],x["repeat"]):x["outcome"] for x in study["summary"]["pilot"]["outcomes"]}
    labels = {"both_pass":("均通过","Both pass","#E4EDF0",INK),
              "both_miss":("均未修好","Both miss","#F1E3DE","#6B3E2C"),
              "bare_only":("仅无技能通过","Bare only","#E9D8D3","#6B3E2C"),
              "manual_only":("仅人工组通过","Manual only","#DAE3F0","#324866"),
              "falsy_rescue":("纠正假值错误","Falsy fix",TEAL,"white")}
    for i in range(3):
        ax.text(i,3.76,(f"第 {i+1} 轮" if zh else f"Repeat {i+1}"),ha="center",va="center",fontsize=10,color=MUTED)
    for j, (key, name) in enumerate(names.items()):
        y = 3 - j
        ax.text(-1.6,y,name,ha="left",va="center",fontsize=12,color=INK,weight="bold")
        for i in range(3):
            cn,en,fill,ink = labels[cells[key,i]]
            ax.barh(y,width=.92,left=i-.46,height=.77,color=fill)
            ax.text(i,y,cn if zh else en,ha="center",va="center",fontsize=10,color=ink)
    fig.text(.055,.06,"绿色格要求无技能补丁通过公开与普通输入检查，且人工组修好了同一假值边界。"
             if zh else "A falsy fix requires a submitted baseline patch that passed the public and control checks.",
             fontsize=9,color=MUTED)
    return fig


def transfer_repairs(study, lang, *, objects=False):
    zh = lang == "zh"
    if not study["dev"]:
        return None
    title = ("调用方对象开发题上的修复数" if zh else "Repairs on caller-owned object tasks") if objects else (
        "新配置题上的修复数" if zh else "Repairs on fresh configuration tasks")
    fig = canvas(3.25 if objects else 3.0,title)
    tasks = len({row["task"] for row in study["dev"]})
    fig.text(.055,.76,f"相同的 {tasks} 道开发题各做两轮，数字来自独立评分" if zh else
             f"The same {tasks} tasks ran twice. Cells show independently graded repairs.",
             color=MUTED, fontsize=10)
    names = {"bare":("无技能","No skill"),"manual":("人工参照","Manual reference"),
             "learned":("学得技能 · 正常路由","Learned · normal routing"),
             "forced_learned":("学得技能 · 强制注入","Learned · forced")}
    arms = list(names) if objects else list(names)[:3]
    ax = fig.add_axes([.055,.10,.9,.58])
    ax.set(xlim=(-2.0,2.55),ylim=(-.5,len(arms)+.55))
    ax.set_axis_off()
    cols = [("第 1 轮","Repeat 1"),("第 2 轮","Repeat 2"),("总计","Total")]
    for i, labels in enumerate(cols):
        ax.text(i,len(arms)+.12,labels[0 if zh else 1],color=MUTED,va="center",ha="center",fontsize=10)
    for j,arm in enumerate(arms):
        y = len(arms) - j - 1
        ax.text(-2,y,names[arm][0 if zh else 1],va="center",fontsize=11.2,color=INK)
        for i in range(3):
            trials = [r for r in study["dev"] if i==2 or r["repeat"]==i]
            n = sum(r[arm]["passed"] for r in trials)
            ax.barh(y,.88,left=i-.44,height=.74,color="#E3EBEF" if i<2 else "#D6ECE8")
            ax.text(i,y,f"{n}/{len(trials)}",color=TEAL if i==2 else INK,weight="bold",va="center",ha="center",fontsize=12)
    return fig


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=HERE / "optional-control.json")
    parser.add_argument("--transfer", type=Path, default=HERE / "falsy-transfer.json")
    parser.add_argument("--objects", type=Path, default=HERE / "falsy-objects.json")
    parser.add_argument("--output-dir", type=Path, default=IMAGES)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = json.loads((HERE / "overview.json").read_text(encoding="utf-8"))
    uptake = json.loads((HERE / "behavior-uptake.json").read_text(encoding="utf-8"))
    optional = json.loads(args.study.read_text(encoding="utf-8"))
    transfer = json.loads(args.transfer.read_text(encoding="utf-8"))
    objects = json.loads(args.objects.read_text(encoding="utf-8")) if args.objects.is_file() else None
    for language, text in TEXT.items():
        with plt.rc_context({"font.family": text["font"]}):
            for name, render, dataset in [("mock-repairs", mock_chart, "mock_demo"),
                                          ("glm-development", live_chart, "live_study")]:
                chart = render(result[dataset], text)
                chart.savefig(args.output_dir / f"{name}-{language}.png", dpi=180, metadata={"Software": "Matplotlib"})
                plt.close(chart)
            charts = [("behavior-uptake", uptake_chart(uptake, language)),
                      ("diversity-gain", diversity_chart(optional, language)),
                      ("falsy-pretest", pretest_chart(transfer, language)),
                      ("falsy-dev", transfer_repairs(transfer,language)),
                      ("falsy-diversity-gain", diversity_chart(transfer, language))]
            if objects:
                charts.extend([("falsy-objects",transfer_repairs(objects,language,objects=True)),
                               ("falsy-object-diversity",diversity_chart(objects,language))])
            for name, chart in charts:
                if chart is not None:
                    chart.savefig(args.output_dir / f"{name}-{language}.png", dpi=180, metadata={"Software": "Matplotlib"})
                    plt.close(chart)
