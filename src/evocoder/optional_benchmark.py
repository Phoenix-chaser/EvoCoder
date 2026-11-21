"""Parametric, executable controls for optional configuration values. Grader assets stay outside solver files."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from evocoder.contracts import Task, canonical, digest
from evocoder.runtime import validation


@dataclass(frozen=True)
class Scenario:
    slug: str
    split: str
    shape: str
    fields: tuple[str, ...]
    program: str
    broken: str
    reference: str
    broad: str


SCENARIOS = (
    Scenario("dispatch", "learn", "scalar", ("retries", "compress", "queue", "labels", "headers"),
             """def handle(request):
    option = request.get(FIELD)
    value = SELECT
    return {"dispatch": {FIELD: value}}
""", "option", "DEFAULT if option is None else option", "option or DEFAULT"),
    Scenario("export", "learn", "scalar", ("row_limit", "quote_all", "prefix", "columns", "aliases"),
             """class ExportPlan:
    def __init__(self, option=DEFAULT):
        self.option = option

    def manifest(self):
        return {"export": {FIELD: self.option}}

def handle(request):
    supplied = request.get(FIELD)
    plan = SELECT
    return plan.manifest()
""", "ExportPlan(supplied)", "ExportPlan() if supplied is None else ExportPlan(supplied)", "ExportPlan(supplied or DEFAULT)"),
    Scenario("gateway", "dev", "scalar", ("retry_limit", "buffered", "route_tag", "zones", "annotations"),
             """from dataclasses import dataclass, replace

@dataclass(frozen=True)
class Policy:
    value: object

def handle(request):
    preset = Policy(DEFAULT)
    override = request.get(FIELD)
    active = SELECT
    return {"gateway": {FIELD: active.value}}
""", "replace(preset, value=override)", "preset if override is None else replace(preset, value=override)",
             "replace(preset, value=override or preset.value)"),
    Scenario("audit", "dev", "scalar", ("retention", "publish", "category", "recipients", "attributes"),
             """def field_updates(options):
    item = options.get(FIELD)
    return SELECT

def handle(request):
    record = {FIELD: DEFAULT}
    record.update(field_updates(request))
    return {"audit": record}
""", "{FIELD: item}", "{} if item is None else {FIELD: item}", "{FIELD: item} if item else {}"),
    Scenario("cache", "test", "scalar", ("ttl", "track_hits", "namespace", "replicas", "metadata"),
             """def cache_options(config):
    values = [(FIELD, DEFAULT)]
    value = config.get(FIELD)
    SELECT
    return dict(values)

def handle(request):
    return {"cache": cache_options(request)}
""", "values.append((FIELD, value))", "if value is not None:\n        values.append((FIELD, value))",
             "if value:\n        values.append((FIELD, value))"),
    Scenario("report", "test", "scalar", ("max_rows", "summarize", "caption", "sections", "styles"),
             """def header(config):
    try:
        raw = config[FIELD]
    except KeyError:
        raw = None
    return FIELD, SELECT

def handle(request):
    return {"report": dict([header(request)])}
""", "raw", "DEFAULT if raw is None else raw", "raw or DEFAULT"),
    Scenario("notify", "test", "layers", ("attempts", "threaded", "topic", "channels", "routing"),
             """def select(local, tenant):
    for layer in (local, tenant):
        if SELECT:
            return layer[FIELD]
    return DEFAULT

def handle(request):
    value = select(request["request"], request["tenant"])
    return {"notify": {FIELD: value}}
""", "FIELD in layer", "layer.get(FIELD) is not None", "layer.get(FIELD)"),
    Scenario("renderer", "test", "layers", ("padding", "paginate", "theme", "panels", "tokens"),
             """def handle(request):
    output = {FIELD: DEFAULT}
    for layer in (request["tenant"], request["request"]):
        patch = {k: v for k, v in layer.items() if SELECT}
        output.update(patch)
    return {"renderer": output}
""", "k == FIELD", "k == FIELD and v is not None", "k == FIELD and v"),
    Scenario("events", "test", "batch", ("priority", "indexed", "source", "topics", "metadata"),
             """def encode(row):
    configured = row.get(FIELD)
    return {FIELD: SELECT}

def handle(request):
    return {"events": [encode(row) for row in request["rows"]]}
""", "configured", "DEFAULT if configured is None else configured", "configured or DEFAULT"),
    Scenario("checkpoint", "test", "batch", ("position", "emit", "stream", "segments", "labels"),
             """def handle(request):
    records = []
    for change in request["rows"]:
        saved = dict.fromkeys((FIELD,), DEFAULT)
        updates = ((k, v) for k, v in change.items() if SELECT)
        saved.update(updates)
        records.append(saved)
    return {"checkpoint": records}
""", "k == FIELD", "k == FIELD and v is not None", "k == FIELD and v"),
)

# These five wire types represent distinct API contracts in each domain.
VALUES = ((5, 12, 0, 19), (True, True, False, True), ("standard", "custom", "", "second"),
          (["base"], ["one", "two"], [], ["other"]), ({"mode": "base"}, {"new": 1}, {}, {"extra": 2}))


def expected(scenario, key, default, request):
    def first(scopes):
        return next((scope[key] for scope in scopes if key in scope and scope[key] is not None), default)
    if scenario.shape == "batch":
        return {scenario.slug: [{key: first([row])} for row in request["rows"]]}
    scopes = [request["request"], request["tenant"]] if scenario.shape == "layers" else [request]
    return {scenario.slug: {key: first(scopes)}}


def requests(shape, key, sample, empty, other):
    typical, zero = {key: sample}, {key: empty}
    absent, null = {}, {key: None}
    if shape == "layers":
        def wrap(a, b):
            return {"request": a, "tenant": b}
        return ([wrap(typical, absent), wrap(null, {key: other}), wrap(absent, absent)],
                [wrap(zero, typical), wrap(null, zero), wrap(typical, zero), wrap(null, null),
                 wrap({"unrelated": 3}, zero), wrap(absent, {key: other})])
    if shape == "batch":
        return ([{"rows": [typical, null]}, {"rows": [absent]}],
                [{"rows": [zero, null, typical, zero]}, {"rows": []}, {"rows": [{"unrelated": 1}, {key: other}, zero]}])
    return ([typical, absent, null], [zero, {"unrelated": "keep"}, {key: other}, {key: empty, "unrelated": 7}])


def checks(cases):
    return ("import copy\nimport json\nfrom service import handle\n\n"
            f"CASES = {cases!r}\n\n"
            "for request, expected in CASES:\n"
            "    original = copy.deepcopy(request)\n"
            "    actual = handle(request)\n"
            "    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True), "
            "(request, expected, actual)\n"
            "    assert request == original, 'input changed'\n")


def instance(scenario, index):
    field = scenario.fields[index]
    default, sample, empty, other = VALUES[index]
    public, private = requests(scenario.shape, field, sample, empty, other)
    if scenario.split == "learn":
        public = [*public, private[0]]
    def cases(inputs):
        return [(r, expected(scenario, field, default, r)) for r in inputs]
    header = f"FIELD = {field!r}\nDEFAULT = {default!r}\n\n"
    def source(fragment):
        return header + scenario.program.replace("SELECT", fragment)
    issue = (f"The {scenario.slug} adapter returns an unexpected {field} setting when a client omits its "
             f"override or sends a null setting. Restore the documented preset behavior. "
             "Keep existing client-supplied settings and the output format compatible.")
    interface = {"scalar": "One settings object overrides the preset.",
                 "layers": "Request overrides take precedence over tenant settings and the factory preset, in that order.",
                 "batch": "Each row independently overrides the preset. Preserve row order and the number of records."}
    task = Task(id=f"{scenario.slug}-{index + 1:02}", split=scenario.split, repository=f"adapters/{scenario.slug}",
                source_group=scenario.slug, issue=issue, editable=["service.py"],
                files={"service.py": source(scenario.broken), "public_tests.py": checks(cases(public)),
                       "README.md": (f"# {scenario.slug} adapter\n\n{interface[scenario.shape]} "
                                     f"The preset for {field} is {default!r}. Return JSON-compatible values with their "
                                     "original wire types. Processing must leave the input intact.\n")},
                public_command=["python", "public_tests.py"],
                grader_files={"grade_contract.py": checks(cases(public + private))},
                grader_command=["python", "grade_contract.py"])
    return task, source(scenario.reference), source(scenario.broad)


def catalog():
    return [(s, *instance(s, i)) for s in SCENARIOS for i in range(len(s.fields))]


def freeze_dataset(root: Path):
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for scenario, task, reference, broad in catalog():
        name = task.id + ".json"
        (root / name).write_text(canonical(task.model_dump()) + "\n")
        rows.append({"id": task.id, "path": name, "hash": digest(task.model_dump()), "split": task.split,
                     "source_group": task.source_group, "family": scenario.shape,
                     "reference_hash": digest(reference), "truthiness_mutant_hash": digest(broad)})
    manifest = {"schema": 1, "kind": "generated_optional_configuration_controls", "tasks": rows}
    (root / "manifest.json").write_text(canonical(manifest) + "\n")
    return manifest


def calibrate(item, settings):
    _, task, reference, broad = item
    files = {}
    for i, implementation in enumerate((task.files["service.py"], reference, broad)):
        for path, src in {**task.files, **task.grader_files, "service.py": implementation}.items():
            files[f"v{i}/{path}"] = src
    files["calibrate.py"] = (
        "import json, subprocess, sys\n"
        "print(json.dumps([[subprocess.run([sys.executable, '-B', script], cwd='v'+str(v), "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode for script in "
        "('public_tests.py','grade_contract.py')] for v in range(3)]))\n")
    result = validation(files, ["python", "calibrate.py"], settings, task.id + "-cal")
    codes = json.loads(result["output"]) if result["exit_code"] == 0 else None
    expected_codes = [[1, 1], [0, 0], [int(task.split == "learn"), 1]]
    if codes != expected_codes or result["image_id"] != settings.image:
        raise ValueError(f"calibration failed on {task.id}: {codes}")
    return {"id": task.id, "hash": digest(task.model_dump()), "exit_codes": codes, "image": result["image_id"]}
