"""Fresh development cases for the caller-owned object boundary observed in the pretest."""
from evocoder.contracts import Task

# Hand-written public contracts. None denotes absence, while the supplied object may define
# __bool__ or __len__. Reference edits and the named boundary programs stay evaluator-side.
CASES = {
    "index-error": {
        "issue": "A missing record with a caller-selected error can raise another exception while presenting "
                 "its key. Respect the documented error policy for lookups and empty selections.",
        "readme": "RecordIndex.get and RecordIndex.first accept a failure instance. They raise that exact "
                  "instance when the result is absent. Omitting it uses the normal lookup diagnostics.",
        "files": {"index.py": """class RecordIndex:
    def __init__(self, records):
        self.records = dict(records)

    def get(self, key, failure=None):
        if key in self.records:
            return self.records[key]
        message = 'no record ' + repr(key)
        raise failure or LookupError(message)

    def first(self, keys, failure=None):
        for key in keys:
            if key in self.records:
                return self.records[key]
        return self._empty(failure)

    def _empty(self, chosen):
        if chosen:
            raise chosen
        raise LookupError('no matching records')
"""},
        "edits":[("index.py", "message = 'no record ' + repr(key)\n        raise failure or LookupError(message)",
                  "if failure is not None:\n            raise failure\n        raise LookupError('no record ' + repr(key))"),
                 ("index.py","if chosen:","if chosen is not None:")],
        "setup": """from index import RecordIndex
class Unrenderable:
    def __repr__(self):
        raise ArithmeticError('deferred presentation is unavailable')
def raises_same(error, call):
    try:
        call()
    except Exception as got:
        assert got is error, type(got).__name__
    else:
        raise AssertionError('expected an error')
index = RecordIndex({'saved': 0})
""",
        "public":"e=ValueError('chosen')\nraises_same(e,lambda: index.get(Unrenderable(),e))\n",
        "controls": """assert index.get('saved') == index.first(['saved']) == 0
e = ValueError('chosen')
raises_same(e,lambda:index.get('other',e))
raises_same(e,lambda:index.first([],e))
for fn, message in [(lambda:index.first([]),'no matching records'),(lambda:index.get(7),'no record 7')]:
    try:
        fn()
    except LookupError as got:
        assert str(got) == message
    else:
        raise AssertionError('expected default error')
""",
        "falsy_setup":"class Quiet(ValueError):\n    def __bool__(self): return False\ne=Quiet('chosen')\n",
        "targets":{"missing_key":"raises_same(e,lambda:index.get('absent',e))",
                   "empty_selection":"raises_same(e,lambda:index.first([],e))"},
    },
    "label-policy": {
        "issue":"Offline label previews sometimes consult a missing catalog even when a label policy is supplied. "
                "Preserve the caller's label policy for both ordinary batches and the empty batch caption.",
        "readme":"A formatter can supply a label policy callable. It accepts a category and returns the printed "
                 "name. Without one a catalog supplies the policy. The category for an empty batch is 'empty'.",
        "files":{"labels.py":"""from footer import empty_caption

def render(categories, catalog, policy=None):
    if not categories:
        return [empty_caption(catalog, policy)]
    default = catalog['formatter']
    chosen = policy or default
    return [chosen(x) for x in categories]
""","footer.py":"""def empty_caption(catalog, configured):
    if configured:
        callback = configured
    else:
        callback = catalog['formatter']
    return callback('empty')
"""},
        "edits":[("labels.py","default = catalog['formatter']\n    chosen = policy or default",
                  "chosen = catalog['formatter'] if policy is None else policy"),("footer.py","if configured:","if configured is not None:")],
        "setup":"from labels import render\nbase = {'formatter':lambda category: 'stock-'+category}\n",
        "public":"assert render(['a'],{},lambda category:'local-'+category) == ['local-a']\n",
        "controls":"""assert render(['a'],base) == ['stock-a']
assert render([],base,None) == ['stock-empty']
assert render([],base,lambda c:c.upper()) == ['EMPTY']
assert render(['a','b'],base,lambda c:c) == ['a','b']
""",
        "falsy_setup":"class Labels(dict):\n    def __call__(self,category): return 'custom-'+category\nfn=Labels()\n",
        "targets":{"nonempty_batch":"assert render(['a'],base,fn) == ['custom-a']",
                   "empty_batch":"assert render([],base,fn) == ['custom-empty']"},
    },
    "result-sink": {
        "issue":"A detached in-memory report with an explicit output sink still tries to allocate its usual "
                "destination. Respect the selected output object for single records and for finalizing a batch.",
        "readme":"emit and close_batch return the destination object they used. A caller may supply a destination "
                 "before it has accumulated records. A missing destination comes from the session.",
        "files":{"session.py":"""class Session:
    def __init__(self, attached=True):
        self.attached = attached
        self.default = []

    def destination(self):
        if not self.attached:
            raise RuntimeError('detached session has no default output')
        return self.default
""","report.py":"""def emit(session, value, sink=None):
    target = session.destination()
    if sink:
        target = sink
    target.append(('value', value))
    return target

def close_batch(session, values, sink=None):
    if values:
        for value in values:
            sink = emit(session, value, sink)
        return sink
    out = sink or session.destination()
    out.append(('end', 0))
    return out
"""},
        "edits":[("report.py","target = session.destination()\n    if sink:\n        target = sink",
                  "target = session.destination() if sink is None else sink"),
                 ("report.py","out = sink or session.destination()","out = session.destination() if sink is None else sink")],
        "setup":"from report import emit,close_batch\nfrom session import Session\n",
        "public":"out=['ready']\nassert emit(Session(False),4,out) is out\nassert out[-1] == ('value',4)\n",
        "controls":"""s=Session()
assert emit(s, 0) is s.default
assert s.default == [('value',0)]
assert close_batch(s,[],None) is s.default
existing=['ready']
assert close_batch(s,[1,2],existing) is existing
assert existing[-2:] == [('value',1),('value',2)]
assert close_batch(s,[],existing) is existing
""",
        "falsy_setup":"s=Session()\ntarget=[]\n",
        "targets":{"empty_destination":"assert emit(s,3,target) is target and target == [('value',3)] and not s.default",
                   "empty_finalization":"assert close_batch(s,[],target) is target and target == [('end',0)] and not s.default"},
    }
}


def build(name):
    case = CASES[name]
    files = case["files"]
    repair = files.copy()
    for path, old, new in case["edits"]:
        if repair[path].count(old) != 1:
            raise ValueError("ambiguous evaluator reference")
        repair[path] = repair[path].replace(old,new)
    tests = {"controls":case["controls"], **{k:case["falsy_setup"]+v+"\n" for k,v in case["targets"].items()}}
    hidden = {f"probe_{k}.py":case["setup"] + body for k,body in tests.items()}
    hidden["grade.py"] = "import runpy\n" + "".join(f"runpy.run_path({k!r})\n" for k in hidden)
    repo = "object-contracts/" + name
    t = Task(id="object-"+name,repository=repo,source_group=repo,split="dev",issue=case["issue"],editable=list(files),
             files={**files, "README.md":case["readme"]+"\n","public_test.py":case["setup"]+case["public"]},
             public_command=["python","public_test.py"],grader_files=hidden,grader_command=["python","grade.py"])
    return t,repair
