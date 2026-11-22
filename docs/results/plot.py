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


def object_revision_chart(studies, lang):
    """Show concurrent controls separately for each proposal and which normally routed skills were used."""
    zh = lang == "zh"
    cohorts = [(label, data) for label, data in studies if data and "objects" in data["summary"]]
    if not cohorts:
        return None
    fig = canvas(4.25, "对象题上的技能修订" if zh else "Skill revisions on object tasks")
    fig.text(.055, .80, "每格是独立评分修好的次数。三题各两轮，每列使用同轮对照。"
             if zh else "Successful repairs across three tasks and two repeats, with concurrent controls.", fontsize=10, color=MUTED)
    ax = fig.add_axes([.055, .14, .92, .58])
    ax.set(xlim=(-2.65, 2.45), ylim=(-.5, 6.7))
    ax.set_axis_off()
    names = [("bare","无技能","No skill"),("parent","父版，正常路由","Parent, normal routing"),
             ("child","子版，正常路由","Child, normal routing"),("fixed_parent","强制使用父版","Parent, forced"),
             ("fixed_child","强制使用子版","Child, forced"),("manual","人工参照","Manual reference")]
    for k, (label, _) in enumerate(cohorts):
        headers = {"first": ("执行轨迹","Execution traces"), "witness": ("配对补丁与新断言","Paired patch check")}
        ax.text(k*1.40, 6.18, headers[label][0 if zh else 1], ha="center", fontsize=10.3, weight="bold", color=INK)
    for j,(arm,cn,en) in enumerate(names):
        y=5-j
        ax.text(-2.65, y, cn if zh else en, va="center", fontsize=10.7, color=INK)
        for k,(_,d) in enumerate(cohorts):
            a=d["summary"]["objects"][arm]
            x=k*1.4
            ax.barh(y,1.27,left=x-.635,height=.78,color="#D6ECE8" if arm in ("child","fixed_child") else "#E3EBEF")
            ax.text(x,y,f"{a['passed']}/{a['trials']}",ha="center",va="center",color=INK,fontsize=12,weight="bold")
    exposure=[f"{d['summary']['objects']['child']['applied']}/6" for _,d in cohorts]
    fig.text(.055,.055,("正常路由实际注入子版（按列）  " if zh else "Child actually used under normal routing, by column   ")
             +"   ·   ".join(exposure),fontsize=10,color=TEAL,weight="bold")
    return fig


def parallel_routing_chart(study, lang):
    zh = lang == "zh"
    fig = plt.figure(figsize=(8.8, 4.3), facecolor=PAPER)
    fig.text(.055, .90, "保留父技能后，GLM 如何选择和修复？" if zh else
             "What changes when both skill versions are available?", fontsize=17, weight="bold", color=INK)
    fig.text(.055, .815, "GLM-5.3   ·   正常路由   ·   同题两轮" if zh else
             "GLM-5.3   ·   Normal routing   ·   Two repeats per task", color=MUTED, fontsize=10.5)
    scores = study["freeze"]["scores"]
    panels = study["summary"]["panels"]
    sections = [
        (.055, .47, "独立学习题上的路由" if zh else "Routing on independent learn tasks",
         ["配置选父版", "异常选子版", "无关题误选"] if zh else ["Config → parent", "Exception → child", "Control misroutes"],
         ["只给题面", "加公开摘录"] if zh else ["Issue only", "+ excerpts"],
         [[f"{scores[v][k]}/4" for v in ("issue", "public_preview")] for k in ("parent", "child", "control_applied")]),
        (.56, .40, "开发题的独立修复评分" if zh else "Graded development repairs",
         ["对象", "配置"] if zh else ["Objects", "Configuration"],
         ["仅父版", "两版并列"] if zh else ["Parent only", "Both skills"],
         [[f"{panels[k][a]['passed']}/{panels[k][a]['trials']}" for a in ("parent","portfolio")] for k in ("objects","config")]),
    ]
    for left, width, title, labels, headers, values in sections:
        ax = fig.add_axes([left, .22, width, .51])
        ax.set_axis_off()
        ax.set(xlim=(0,1), ylim=(-.15,4.15))
        ax.text(0, 3.8, title, fontsize=12, weight="bold", color=INK)
        for j, header in enumerate(headers):
            x = .59 + j * .27
            ax.text(x, 3.08, header, ha="center", color=MUTED, fontsize=9.5)
        for i,(label, cells) in enumerate(zip(labels, values, strict=True)):
            y = 2.23 - i*.9
            ax.text(0,y,label,va="center",color=INK,fontsize=10.4)
            for j,value in enumerate(cells):
                x = .59 + j * .27
                ax.barh(y,.235,height=.73,left=x-.1175,color="#E2EBEF" if j==0 else "#D4EBE7")
                ax.text(x,y,value,ha="center",va="center",color=INK,fontsize=12,weight="bold")
    chosen = {"issue": ("只给题面","issue only"), "public_preview": ("加公开摘录","public excerpts")}[study["view"]]
    fig.text(.055,.11,("开发前冻结的路由输入　"+chosen[0] if zh else "Routing input frozen before development   ·   "+chosen[1]),
             fontsize=10,weight="bold",color=TEAL)
    return fig


def shape_routing_chart(study, lang):
    zh = lang == "zh"
    fig = plt.figure(figsize=(9.4, 5.0), facecolor=PAPER)
    fig.text(.055, .90, "公开测试形状能帮助选对技能，修复能复现几次？" if zh else
             "Do better routing choices lead to repeatable repairs?", fontsize=17, weight="bold", color=INK)
    fig.text(.055, .82, "GLM-5.3  ·  父子技能文本冻结  ·  模型可选择任一版本或跳过" if zh else
             "GLM-5.3  ·  Frozen parent and child skills  ·  The router could choose either skill or skip",
             fontsize=10.5, color=MUTED)

    left = fig.add_axes([.055, .23, .42, .49])
    left.set_axis_off()
    left.set(xlim=(0, 1), ylim=(-.15, 5.65))
    left.text(0, 5.35, "学习题上的路由判断" if zh else "Choices on learning tasks",
              fontsize=12, weight="bold", color=INK)
    for x, name in zip((.66, .90), ("题面", "加形状") if zh else ("Issue", "+ shapes")):
        left.text(x, 4.62, name, ha="center", color=MUTED, fontsize=9)
    labels = (("新题 · 异常选子", "New · exception → child", "fresh", "child"),
              ("新题 · 配置选父", "New · config → parent", "fresh", "parent"),
              ("新题 · 对照误选", "New · false selections", "fresh", "control_applied"),
              ("复测 · 异常选子", "Revisited · exception → child", "revisited", "child"),
              ("复测 · 配置选父", "Revisited · config → parent", "revisited", "parent"),
              ("复测 · 对照误选", "Revisited · false selections", "revisited", "control_applied"))
    for i, (cn, en, cohort, metric) in enumerate(labels):
        y = 3.98 - .72*i
        left.text(0, y, cn if zh else en, va="center", color=INK, fontsize=9.4)
        for x, v, color in ((.66, "issue", "#E2EBEF"), (.90, "public_shape", "#D4EBE7")):
            left.barh(y, .202, left=x-.101, height=.59, color=color)
            left.text(x, y, str(study["freeze"]["scores"][cohort][v][metric])+"/4", ha="center",
                      va="center", color=INK, fontsize=10.2, weight="bold")

    ax = fig.add_axes([.64, .342, .30, .29], facecolor=PAPER)
    fig.text(.526, .684, "父版失手，子技能救回几轮？" if zh else "When the parent failed",
             fontsize=12, weight="bold", color=INK)
    gate = study["gate"]["decisions"]["objects_vs_parent"]["tasks"]
    items = (("object-index-error","异常对象","Exception"), ("object-label-policy","标签回调","Callback"),
             ("object-result-sink","结果容器","Result sink"))
    for j, (tid, _, _) in enumerate(items):
        y = 2-j
        rows = [r for r in study["dev"] if r["task"] == tid]
        headroom = sum(not r["parent"]["passed"] for r in rows)
        actual = gate[tid]["attributed_wins"]
        ax.barh(y, 5, height=.56, color="#E7EDEF")
        ax.barh(y, headroom, height=.56, color="#A6B8C4")
        ax.barh(y, actual, height=.56, color=TEAL)
        ax.text(5.11, y, f"{actual}/5", va="center", fontsize=9.7, color=INK, weight="bold")
    ax.set(xlim=(0, 5.8), ylim=(-.52, 2.58), xticks=range(6), yticks=[2,1,0],
           yticklabels=[cn if zh else en for _,cn,en in items])
    ax.tick_params(axis="both", length=0, labelsize=9, colors=MUTED)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.axvline(3, linestyle="--", linewidth=1.2, color=INK)
    fig.text(.526,.245,"灰蓝 = 未计为子技能胜例   绿 = 实际选中子版并修好" if zh else
             "Blue-grey = no credited child win   Green = child used and passed", fontsize=9.1, color=MUTED)
    fig.text(.526,.181,"虚线表示每个来源需达到 3/5，发布至少需两个来源" if zh else
             "Dashed line = 3/5 required per source. Release needs two.", fontsize=9.1, color=MUTED)
    n = len(study["gate"]["decisions"]["objects_vs_parent"]["repeat_confirmed_improved_groups"])
    fig.text(.055,.071,(f"对象来源达到重复改善门槛  {n}/3     ·     留出题实际接触  {study['holdout_exposures']}/30" if zh else
             f"Object sources with repeatable gains   {n}/3      ·      Holdout tasks exposed   {study['holdout_exposures']}/30"),
             fontsize=10.8, weight="bold", color=TEAL)
    return fig


def callback_gate_chart(study, lang):
    """Plot actually attributed wins on the five prospectively evaluated object programs."""
    zh = lang == "zh"
    labels = (
        ["标签回调", "延迟打印回执", "结果容器", "索引异常", "迟到事件排序 †"]
        if zh else ["Label callback", "Deferred print receipt", "Result sink", "Index exception", "Late-event ordering †"]
    )
    task_ids = ["object-label-policy", "proof-dispatch", "object-result-sink", "object-index-error", "event-collation"]
    decisions = study["gate"]["decisions"]
    fig = plt.figure(figsize=(8.0, 4.75), facecolor=PAPER)
    title = "实际使用子技能后，同轮多修好了几次？" if zh else "How often did a repair improve after applying the child?"
    fig.text(.055, .91, title, color=INK, fontsize=16, weight="bold")
    fig.text(.055, .83, "每个来源五轮。虚线为每个来源的发布门槛（3/5）。" if zh
             else "Five paired rounds per source. The dashed line marks the 3/5 release threshold.",
             color=MUTED, fontsize=10)
    ax = fig.add_axes([.29, .23, .61, .53], facecolor=PAPER)
    ax.axvline(3, color=MUTED, linestyle="--", linewidth=.9, zorder=0)
    for y in range(5):
        ax.axhline(y, color=TRACK, linewidth=.8, zorder=0)
    for arm, dy, marker, color, key in (("parent", -.12, "o", TEAL, "相对仅父版" if zh else "vs. parent only"),
                                       ("bare", .12, "s", SLATE, "相对无技能" if zh else "vs. no skill")):
        wins = [decisions[arm]["tasks"][tid]["attributed"] for tid in task_ids]
        ax.scatter(wins, [i+dy for i in range(len(wins))], marker=marker, s=57, color=color, label=key, zorder=2)
    ax.set(xlim=(-.25, 5.25), ylim=(4.5, -.55), xticks=range(6), yticks=range(5), yticklabels=labels)
    ax.spines[:].set_visible(False)
    ax.tick_params(length=0, colors=INK)
    ax.legend(loc="upper center", bbox_to_anchor=(.5, -.11), ncol=2, frameon=False, fontsize=10)
    fig.text(.055, .055, "† 排序预试里父版五次全对，该来源只参与退化检查。" if zh
             else "† The parent passed all five ordering pilots. This source only contributes to regression checks.",
             color=MUTED, fontsize=9)
    return fig


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=HERE / "optional-control.json")
    parser.add_argument("--transfer", type=Path, default=HERE / "falsy-transfer.json")
    parser.add_argument("--objects", type=Path, default=HERE / "falsy-objects.json")
    parser.add_argument("--revision", type=Path, default=HERE / "object-revision.json")
    parser.add_argument("--witness", type=Path, default=HERE / "object-witness.json")
    parser.add_argument("--shape", type=Path, default=HERE / "shape-routing.json")
    parser.add_argument("--callback", type=Path, default=HERE / "callback-release.json")
    parser.add_argument("--portfolio", type=Path, default=HERE / "parallel-routing.json")
    parser.add_argument("--output-dir", type=Path, default=IMAGES)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = json.loads((HERE / "overview.json").read_text(encoding="utf-8"))
    uptake = json.loads((HERE / "behavior-uptake.json").read_text(encoding="utf-8"))
    optional = json.loads(args.study.read_text(encoding="utf-8"))
    transfer = json.loads(args.transfer.read_text(encoding="utf-8"))
    objects = json.loads(args.objects.read_text(encoding="utf-8")) if args.objects.is_file() else None
    revisions = [(label,json.loads(path.read_text(encoding="utf-8"))) for label,path in
                 (("first",args.revision),("witness",args.witness)) if path.is_file()]
    portfolio = json.loads(args.portfolio.read_text(encoding="utf-8")) if args.portfolio.is_file() else None
    shape = json.loads(args.shape.read_text(encoding="utf-8")) if args.shape.is_file() else None
    callback = json.loads(args.callback.read_text(encoding="utf-8")) if args.callback.is_file() else None
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
            charts.append(("object-revision",object_revision_chart(revisions,language)))
            charts.extend((f"object-{label}-diversity",diversity_chart(data,language)) for label,data in revisions)
            if portfolio:
                charts.extend([("parallel-routing", parallel_routing_chart(portfolio, language)),
                               ("parallel-diversity", diversity_chart(portfolio, language))])
            if shape:
                charts.extend([("shape-routing", shape_routing_chart(shape, language)),
                               ("shape-diversity", diversity_chart(shape, language))])
            if callback:
                charts.extend([("callback-release", callback_gate_chart(callback, language)),
                               ("callback-diversity", diversity_chart(callback, language))])
            for name, chart in charts:
                if chart is not None:
                    chart.savefig(args.output_dir / f"{name}-{language}.png", dpi=180, metadata={"Software": "Matplotlib"})
                    plt.close(chart)
