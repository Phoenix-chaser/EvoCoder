"""Draw the published workflow outcome and, after complete activation-only testing, the diversity curve."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
INK = "#172B3A"
TEAL = "#007A75"
BLUE = "#496B9C"


def load_report():
    original = json.loads((ROOT / "docs/results/workflow-revision.json").read_text())
    confirmation = json.loads((ROOT / "docs/results/workflow-confirmation.json").read_text())
    return dict(
        gate=confirmation["gate"],
        screen=original["screen"],
        activation=confirmation["activation"],
        holdout_exposures=confirmation["exposures"],
        holdout={"pairs": confirmation["holdout"], "curve": confirmation["curve"]},
    )


def style(ax):
    ax.spines[["left", "right", "top"]].set_visible(False)
    ax.spines["bottom"].set_color("#BBC8CE")
    ax.tick_params(axis="both", length=0, pad=7, colors=INK)
    ax.set_axisbelow(True)


def figure(data, zh):
    def tr(cn, en):
        return cn if zh else en

    plt.rcParams.update(
        {
            "font.family": "Noto Sans CJK SC" if zh else "DejaVu Sans",
            "font.size": 11,
            "text.color": INK,
            "axes.labelcolor": INK,
            "axes.unicode_minus": False,
        }
    )
    label = "zh" if zh else "en"
    if data["gate"]:
        report = data["gate"]["report"]["gate"]
        fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.6), gridspec_kw={"width_ratios": [1, 1.1]})
        fig.subplots_adjust(left=0.07, right=0.975, top=0.70, bottom=0.26, wspace=0.32)
        fig.text(
            0.06,
            0.91,
            tr("对象技能修订后的正式对照", "A revised object skill on the release panel"),
            fontsize=21,
            weight="bold",
        )
        fig.text(
            0.06,
            0.815,
            tr(
                "GLM-5.3 · 正常路由 · 每个比较使用独立求解",
                "GLM-5.3 · Normal routing · Independently executed arms",
            ),
            fontsize=11,
            color="#46606E",
        )
        names = ["object-label-policy", data["screen"]["selected"]]
        positions = [0, 1]
        for j, (base, color, name) in enumerate(
            [("parent", BLUE, tr("仅父版", "Parent only")), ("bare", TEAL, tr("无技能", "No skill"))]
        ):
            vals = [report["comparisons"][base]["tasks"][t]["attributed"] for t in names]
            xs = [p + (j - 0.5) * 0.30 for p in positions]
            b = left.bar(xs, vals, 0.27, label=name, color=color)
            left.bar_label(b, padding=3, fmt="%.0f", fontsize=11)
        left.axhline(3, color="#985A30", linestyle="--", linewidth=1)
        left.set(
            ylim=(0, 5.8),
            yticks=range(6),
            xticks=positions,
            xticklabels=[tr("标签回调", "Label callback"), tr("延迟确认回调", "Deferred ack")],
        )
        left.set_title(
            tr("实际选用新技能的胜轮 / 5", "Wins with the revised skill / 5"),
            loc="left",
            fontweight="bold",
            fontsize=13,
            pad=14,
        )
        left.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2, frameon=False, fontsize=9)
        d = report["direct"]
        values = []
        for base in ("previous", "revised"):
            pr = d["transfer-print-ticket"]["arms"][base]
            sc = d["learn-scheduler-watch"]["arms"][base]
            values.append(
                [pr["selected_specialization"], pr["probes"]["controls"], sc["probes"]["wake_replay"]]
            )
        for j, (vals, color, name) in enumerate(
            zip(
                values,
                [BLUE, TEAL],
                [tr("旧父子库", "Previous portfolio"), tr("修订父子库", "Revised portfolio")],
                strict=True,
            )
        ):
            b = right.bar([p + (j - 0.5) * 0.30 for p in range(3)], vals, 0.27, label=name, color=color)
            right.bar_label(b, padding=3, fmt="%.0f", fontsize=11)
        right.set(
            ylim=(0, 8),
            yticks=[0, 2, 4, 6, 7],
            xticks=range(3),
            xticklabels=[
                tr("布尔误选 / 7", "Flag misroutes / 7"),
                tr("常规通过 / 7", "Print controls / 7"),
                tr("唤醒通过 / 5", "Wake passes / 5"),
            ],
        )
        right.set_title(
            tr("旧库与新库的保护指标", "Protection checks across portfolios"),
            loc="left",
            fontweight="bold",
            fontsize=13,
            pad=14,
        )
        right.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2, frameon=False, fontsize=9)
        for ax in (left, right):
            ax.grid(axis="y", color="#E1E8EB", linewidth=0.7)
            style(ax)
        fig.text(
            0.06,
            0.045,
            tr(
                "回调和调度器使用原五轮。打印配置使用独立复核的七轮。虚线标出单个来源的胜轮阈值。",
                "Callbacks and wake checks use the original five rounds. Print uses seven fresh rounds. The dashed line marks the release threshold.",
            ),
            fontsize=9,
            color="#506973",
        )
        fig.savefig(ROOT / f"docs/images/workflow-revision-{label}.png", dpi=145, facecolor="white")
        plt.close(fig)
    hold = data.get("holdout")
    if not hold or not hold["curve"]:
        return
    assert (
        data["activation"]
        and data["holdout_exposures"] == 30
        and len(hold["pairs"]) == 30
        and len(hold["curve"]) == 3
    )
    fig, ax = plt.subplots(figsize=(8, 4.8))
    fig.subplots_adjust(left=0.13, right=0.96, top=0.75, bottom=0.19)
    pts = hold["curve"]
    xs = [1, 2, 3]
    fig.suptitle(
        tr("冻结技能库的收益随配置任务族多样性的变化", "Frozen-library gain across configuration strata"),
        weight="bold",
        fontsize=17,
    )
    for key, color, legend, offset, marker, line in [
        ("paired_gain", TEAL, tr("累计混合", "Cumulative mix"), 8, "o", "-"),
        ("family_gain", BLUE, tr("新增任务族", "Added stratum"), -17, "s", "--"),
    ]:
        ys = [100 * p[key] for p in pts]
        ax.plot(
            xs, ys, color=color, label=legend, marker=marker, linestyle=line, linewidth=2,
            markersize=7 if key == "paired_gain" else 10,
            markerfacecolor=color if key == "paired_gain" else "none",
        )
        for x, y in zip(xs, ys):
            ax.annotate(
                f"{y:+.1f}", (x, y), xytext=(0, offset), textcoords="offset points",
                ha="center", fontsize=10, color=color,
            )
    ax.axhline(0, color="#9AA7A9", lw=0.9, zorder=0)
    ax.set_xticks(
        xs,
        [
            tr("单值 · 10 对", "Scalar · 10 pairs"),
            tr("加入多层 · 20 对", "Add layered · 20 pairs"),
            tr("加入批量 · 30 对", "Add batch · 30 pairs"),
        ],
    )
    ax.set_xlim(0.8, 3.2)
    ax.margins(y=0.25)
    ax.set_ylabel(tr("成对通过率差（百分点）", "Paired pass-rate difference (pp)"))
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=2, frameon=False)
    style(ax)
    fig.savefig(ROOT / f"docs/images/workflow-diversity-{label}.png", dpi=145, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    data = load_report()
    for chinese in (True, False):
        figure(data, chinese)
