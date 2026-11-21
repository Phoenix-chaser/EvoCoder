"""Model-blind eligibility checks. Retain every rejection before freezing a benchmark."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from evocoder.contracts import Settings, Task, canonical, digest
from evocoder.sandbox import Sandbox, bounded_process

ROOT = Path("work/benchmark-candidates")
OUT = Path("work/benchmark-verification")
XML_SUMMARY = """import json,xml.etree.ElementTree as E
r=E.parse('/tmp/proof.xml').getroot()
s=list(r.iter('testsuite'))
print(json.dumps({k:sum(int(x.get(k,0)) for x in s) for k in ['tests','failures','errors','skipped']}))
"""


def check(files, command, options, label):
    with Sandbox(files, options, label) as box:
        result = box.execute([*command, "--junitxml=/tmp/proof.xml"])
        xml = box.execute(["python", "-c", XML_SUMMARY])
        proof = json.loads(xml["output"]) if xml["exit_code"] == 0 else None
        return {**result, "image_id": box.image_id, "junit": proof}


def verify(row, options):
    folder = ROOT / row["id"]
    target = OUT / (row["id"] + ".json")
    if target.exists():
        return json.loads(target.read_text())
    task = Task.model_validate_json((folder / "task.json").read_text())
    controller = json.loads((folder / "controller.json").read_text())
    result = {"id": task.id, "repository": task.repository, "passed": False, "checks": {}}
    try:
        assert digest(task.model_dump()) == controller["task_hash"]
        for name, files, cmd in [
            ("buggy_regression", {**task.files, **task.grader_files}, task.grader_command),
            (
                "reference_regression",
                {**task.files, **task.grader_files, **controller["reference"]},
                task.grader_command,
            ),
            ("reference_public", {**task.files, **controller["reference"]}, task.public_command),
            ("buggy_public", task.files, task.public_command),
        ]:
            r = check(files, cmd, options, task.id + "-" + name)
            result["checks"][name] = r
            j = r["junit"]
            if j is None or j["errors"] or not j["tests"] or j["tests"] == j["skipped"]:
                raise ValueError(name + ": missing/assertion-free or collection/import error")
            if name == "buggy_regression" and not (r["exit_code"] == 1 and j["failures"] > 0):
                raise ValueError("original source does not reproduce an assertion failure")
            if name.startswith("reference") and r["exit_code"] != 0:
                raise ValueError(name + ": reference does not pass")
            if name == "buggy_public" and r["exit_code"] not in {0, 1}:
                raise ValueError("original public suite has environment errors")
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:700]}"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    image = bounded_process(
        ["docker", "image", "inspect", "evocoder-comparison:py312-v1", "--format", "{{.Id}}"], 10
    )
    if image["exit_code"] != 0:
        raise ValueError("build comparison image first")
    options = Settings(_env_file=None, mode="mock", image=image["output"].strip())
    rows = json.loads((ROOT / "index.json").read_text())["candidates"]
    legacy = json.loads(Path("benchmarks/realworld/cases.json").read_text())["cases"]
    old = {c["fixed_commit"] for c in legacy}
    rows = [
        r
        for r in rows
        if json.loads((ROOT / r["id"] / "controller.json").read_text())["fixed_commit"] not in old
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(verify, r, options) for r in rows]
        results = []
        for f in as_completed(pending):
            result = f.result()
            results.append(result)
            print(
                json.dumps(
                    {
                        "done": len(results),
                        "passed": sum(r["passed"] for r in results),
                        "id": result["id"],
                        "eligible": result["passed"],
                        "error": result.get("error"),
                    }
                ),
                flush=True,
            )
    (OUT / "summary.json").write_text(
        canonical(
            {
                "image": options.image,
                "rows": results,
                "model_calls": 0,
                "passed": sum(r["passed"] for r in results),
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
