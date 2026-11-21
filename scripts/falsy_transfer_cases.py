"""Independent pretests and fresh development contracts for the optional-value transfer study."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from evocoder.contracts import Task
from evocoder.realworld import download_snapshot

# Only these paths at exact upstream revisions ever enter the solver's workspace.
UPSTREAM = {
    "miss-handler": ("mahmoud/boltons", "bbadc7ae1dcfbb0e71ec3a852e8bca3a6f75199b",
                     "8408ab57e6c92b4ddd37db6220628ea41ee8bb5b", "",
                     ["boltons/cacheutils.py", "boltons/__init__.py", "boltons/typeutils.py"]),
    "cardinality": ("more-itertools/more-itertools", "9ed3dbb0ae527230cd156d91d0af305478558fba",
                    "def2dabea858b6ecb84ee0c52e6e07929f2c409c", "",
                    ["more_itertools/more.py", "more_itertools/recipes.py", "more_itertools/__init__.py"]),
    "size-hook": ("tkem/cachetools", "110fe5d03e7b4b8f5c5ec97533c94254cdf8733f",
                  "ffe21c44d3f7de9c7a89870c8cf1fd7aa583a2ae", "src/",
                  ["cachetools/__init__.py", "cachetools/keys.py", "cachetools/_cached.py", "cachetools/_cachedmethod.py"]),
}

PILOT = ["miss-handler", "cardinality", "size-hook", "queue-packet"]
DEV = ["meter-view", "column-caption", "print-ticket", "palette-book"]
FALSE_EXCEPTION = "class QuietError(Exception):\n    def __bool__(self):\n        return False\ne = QuietError()\n"


def upstream_source(name, cache):
    repo, base, fix, prefix, paths = UPSTREAM[name]
    checkout = cache / repo.replace("/", "--")
    def snapshot(ref, wanted):
        if (checkout / ".git").exists() and subprocess.run(
            ["git", "-C", str(checkout), "cat-file", "-e", ref + "^{commit}"], capture_output=True
        ).returncode == 0:
            return {p: subprocess.check_output(["git", "-C", str(checkout), "show", f"{ref}:{prefix}{p}"]).decode()
                    for p in wanted}
        archive = download_snapshot(repo, ref, cache / "transfer-archives")
        return {p: archive[prefix + p] for p in wanted}
    return snapshot(base, paths), snapshot(fix, paths[:1])


@dataclass
class Spec:
    issue: str
    readme: str
    setup: str
    public: str
    controls: str
    falsy: dict[str, str]


SPECS = {
    "miss-handler": Spec(
        "A configured miss handler can disappear when looking up a new key in the bounded caches. "
        "Both eviction policies accept the same optional handler. Keep the hit and miss accounting intact.",
        "", """from boltons.cacheutils import LRI, LRU
class Lookup(dict):
    def __call__(self, key):
        return key.upper()
""", """c = LRU(max_size=4, on_miss=Lookup())
assert c['oak'] == 'OAK'
""", """for cls in (LRI, LRU):
    cache = cls(max_size=4)
    try:
        cache['absent']
    except KeyError:
        pass
    else:
        raise AssertionError('missing key should raise')
    cache = cls(max_size=4, on_miss=lambda key: 0)
    assert cache['a'] == cache['a'] == 0
    assert (cache.hit_count, cache.miss_count) == (1, 1)
""", {name: f"c = {cls}(max_size=3, on_miss=Lookup())\nassert c['x'] == c['x'] == 'X'\n"
      for name, cls in (("insertion_policy", "LRI"), ("recency_policy", "LRU"))}),
    "cardinality": Spec(
        "A caller's custom cardinality error can be lost when formatting an unprintable iterator element. "
        "Preserve supplied error instances across the one-item helpers and retain the built-in behavior "
        "when callers omit their error.", "", """from more_itertools import one, only
class Unprintable:
    def __repr__(self):
        raise RuntimeError('no presentation for this element')
def same(call, error):
    try:
        call()
    except Exception as caught:
        assert caught is error, type(caught).__name__
    else:
        raise AssertionError('expected an exception')
""", """error = LookupError('too many')
same(lambda: one([Unprintable(), 2], too_long=error), error)
""", """assert one([4]) == only([4]) == 4
assert only([], default=0) == 0
for fn in (one, only):
    try:
        fn([1,2])
    except ValueError as e:
        assert '1' in str(e) and '2' in str(e)
    else:
        raise AssertionError('expected default cardinality error')
error = LookupError('custom')
same(lambda: one([], too_short=error), error)
same(lambda: only([1,2], too_long=error), error)
""", {"empty_one": FALSE_EXCEPTION + "same(lambda: one([], too_short=e), e)\n",
      "overfull_one": FALSE_EXCEPTION + "same(lambda: one([1,2], too_long=e), e)\n",
      "overfull_only": FALSE_EXCEPTION + "same(lambda: only([1,2], too_long=e), e)\n"}),
    "size-hook": Spec(
        "A cache configured with a stateful size function can account for entries as if every value "
        "had unit size. Honor the supplied size policy, including on derived cache implementations.",
        "", """from cachetools import Cache, LRUCache
class Meter:
    def __len__(self):
        return 0
    def __call__(self, item):
        return len(item)
""", """c = Cache(10, getsizeof=Meter())
c[1] = 'abc'
assert c.currsize == 3
""", """for cls in (Cache, LRUCache):
    c = cls(8, getsizeof=None)
    c[1] = 'ab'
    assert c.currsize == 1
    c = cls(8, getsizeof=len)
    c[1] = 'ab'
    assert c.currsize == 2
""", {k: f"c = {cls}(maxsize=8, getsizeof=Meter())\nc['first'] = 'abcd'\nc['second'] = ''\nassert c.currsize == 4\n"
      for k, cls in (("base_meter", "Cache"), ("recency_meter", "LRUCache"))}),
    "queue-packet": Spec(
        "An unset burst limit sometimes reaches a rehearsal as null. Restore the standard queue limit "
        "and keep rehearsals and stored job packets consistent for user-selected limits.",
        "A job's burst limit is a non-negative integer. Unset limits use the queue's standard burst. "
        "A restored packet must schedule the same burst as the rehearsal.", "from service import preview, save, restore\n",
        "assert preview({'burst': None}) == 8\nassert restore(save({'burst': 7})) == 7\n",
        "for obj, value in [({},8), ({'burst':None},8), ({'burst':3},3)]:\n"
        "    assert preview(obj) == restore(save(obj)) == value\n",
        {"immediate_zero": "assert preview({'burst':0}) == 0\n",
         "restored_zero": "assert restore(save({'burst':0})) == 0\n"}),
    "meter-view": Spec(
        "A dashboard opened without a sampling interval can show a blank interval. Restore the documented "
        "preset everywhere the view is used and keep settings compatible with saved dashboards.",
        "The meter accepts an optional integer interval. Zero disables sampling. Reopening a bookmark "
        "must give the same effective interval as the original view.",
        "from service import open_meter, bookmark, reopen\n",
        "assert open_meter({'interval': None}).interval == 60\nassert reopen(bookmark(open_meter({'interval': 5}))).interval == 5\n",
        "for x, n in [({},60), ({'interval':None},60), ({'interval':4},4)]:\n"
        "    view=open_meter(x)\n    assert view.interval == reopen(bookmark(view)).interval == n\n",
        {"direct_zero": "assert open_meter({'interval':0}).interval == 0\n",
         "bookmark_zero": "assert reopen(bookmark(open_meter({'interval':0}))).interval == 0\n"}),
    "column-caption": Spec(
        "An unset column heading becomes an empty cell in the exported header. Use its documented "
        "caption and keep custom column captions consistent between a live export and a saved column specification.",
        "Untitled custom headings are supported. A caption omitted by a client or set to null inherits "
        "the standard heading. A specification loaded from storage must export the same CSV header.",
        "import csv\nfrom service import render, spec, load\ndef first(cfg):\n    return next(csv.reader([render(cfg)]))[0]\n",
        "assert first({'caption':None}) == 'Amount'\nassert first(load(spec({'caption':'Total'}))) == 'Total'\n",
        "for obj, value in [({},'Amount'), ({'caption':None},'Amount'), ({'caption':'a,b'},'a,b')]:\n"
        "    assert first(obj) == first(load(spec(obj))) == value\n",
        {"inline_blank": "assert first({'caption':''}) == ''\n",
         "restored_blank": "assert first(load(spec({'caption':''}))) == ''\n"}),
    "print-ticket": Spec(
        "Printer previews can lose the site duplex preset when a profile contains a null entry. "
        "Preserve explicit print choices when the profile travels in a queued print ticket.",
        "The site defaults to duplex. A profile may request single-sided output. Immediate jobs and jobs "
        "restored from a print ticket must agree on the boolean choice, including its JSON type.",
        "from service import sides, ticket, read_ticket\n",
        "assert sides({'duplex':None}) is True\nassert read_ticket(ticket({'duplex':True})) is True\n",
        "for obj in ({}, {'duplex':None}, {'duplex':True}):\n    assert sides(obj) is True\n"
        "    assert read_ticket(ticket(obj)) is True\n",
        {"local_simplex": "assert sides({'duplex':False}) is False\n",
         "queued_simplex": "assert read_ticket(ticket({'duplex':False})) is False\n"}),
    "palette-book": Spec(
        "An inherited palette may appear as null when building a plotting preset. Restore the factory palette "
        "and preserve the observable series colors when a plot book is saved and reopened.",
        "Each plot accepts a list of color names. An explicit empty palette selects monochrome. Only missing "
        "settings inherit the factory entries. The plot book stores a profile for every plot, including overrides.",
        "from service import colors, dump_book, load_book\n",
        "assert colors({'colors': None}) == ['blue']\nassert load_book(dump_book([{'colors':['red']}])) == [['red']]\n",
        "rows=[{}, {'colors':None}, {'colors':['red','gold']}]\n"
        "assert list(map(colors, rows)) == load_book(dump_book(rows)) == [['blue'],['blue'],['red','gold']]\n",
        {"single_empty": "assert colors({'colors':[]}) == []\n",
         "book_empty": "assert load_book(dump_book([{'colors':['red']},{'colors':[]}])) == [['red'],[]]\n"}),
}

# Each application has its own implementation and public interfaces. The patch replaces only
# the listed fragments to make a deterministic evaluator reference for that specific application.
APPS = {
    "queue-packet": ({"service.py": """from packet import encode, decode

STANDARD_BURST = 8

def preview(settings):
    return settings.get('burst', STANDARD_BURST)

def save(settings):
    return encode(preview(settings))

def restore(blob):
    return decode(blob).get('burst', STANDARD_BURST)
""", "packet.py": """import json

def encode(limit):
    fields = {'burst':limit} if limit else {}
    return json.dumps(fields)

def decode(blob):
    return json.loads(blob)
"""}, [("service.py", "settings.get('burst', STANDARD_BURST)", "STANDARD_BURST if settings.get('burst') is None else settings['burst']"),
       ("packet.py", "if limit else", "if limit is not None else")]),
    "meter-view": ({"service.py": """from dataclasses import dataclass
from bookmarks import pack, unpack
STANDARD_INTERVAL = 60

@dataclass(frozen=True)
class Meter:
    interval: int

def open_meter(options):
    return Meter(options.get('interval', STANDARD_INTERVAL))

def bookmark(view):
    return pack(view)

def reopen(fragment):
    options = unpack(fragment)
    return open_meter(options)
""", "bookmarks.py": """from urllib.parse import urlencode, parse_qs

def pack(view):
    fields = [('interval', view.interval)] if view.interval else []
    return urlencode(fields)

def unpack(fragment):
    return {k: int(values[-1]) for k, values in parse_qs(fragment).items()}
"""}, [("service.py", "options.get('interval', STANDARD_INTERVAL)", "STANDARD_INTERVAL if options.get('interval') is None else options['interval']"),
       ("bookmarks.py", "if view.interval else", "if view.interval is not None else")]),
    "column-caption": ({"service.py": """import csv
import io
from catalog import spec, load

CAPTION = 'Amount'

def render(settings):
    stream = io.StringIO(newline='')
    csv.writer(stream).writerow([settings.get('caption', CAPTION), 'Units'])
    return stream.getvalue().strip('\\r\\n')
""", "catalog.py": """import json

def spec(settings):
    pairs = [(k, v) for k, v in settings.items() if k == 'caption' and v]
    return json.dumps(pairs)

def load(payload):
    return dict(json.loads(payload))
"""}, [("service.py", "settings.get('caption', CAPTION)", "CAPTION if settings.get('caption') is None else settings['caption']"),
       ("catalog.py", "and v]", "and v is not None]")]),
    "print-ticket": ({"service.py": """from spool import ticket, read_ticket

SITE_DUPLEX = True

def sides(profile):
    return profile.get('duplex', SITE_DUPLEX)
""", "spool.py": """import json

def ticket(profile):
    flags = []
    if profile.get('duplex'):
        flags.append(['duplex', profile['duplex']])
    return json.dumps({'flags':flags})

def read_ticket(raw):
    from service import SITE_DUPLEX
    cfg = dict(json.loads(raw)['flags'])
    return cfg.get('duplex', SITE_DUPLEX)
"""}, [("service.py", "profile.get('duplex', SITE_DUPLEX)", "SITE_DUPLEX if profile.get('duplex') is None else profile['duplex']"),
       ("spool.py", "if profile.get('duplex'):", "if profile.get('duplex') is not None:")]),
    "palette-book": ({"service.py": """import json

FACTORY = ('blue',)

def colors(plot):
    return plot.get('colors', list(FACTORY))

def dump_book(plots):
    profiles = []
    for plot in plots:
        palette = plot.get('colors')
        profiles.append({'palette': list(palette)} if palette else {})
    return json.dumps(profiles)

def load_book(data):
    return [row.get('palette', list(FACTORY)) for row in json.loads(data)]
"""}, [("service.py", "plot.get('colors', list(FACTORY))", "list(FACTORY) if plot.get('colors') is None else plot['colors']"),
       ("service.py", "if palette else", "if palette is not None else")]),
}


def make_case(name: str, cache: Path):
    s = SPECS[name]
    if name in UPSTREAM:
        files, reference = upstream_source(name, cache)
        repository = UPSTREAM[name][0]
    else:
        inputs, edits = APPS[name]
        files = {**inputs, "README.md": s.readme + "\n"}
        reference = inputs.copy()
        for path, old, new in edits:
            if reference[path].count(old) != 1:
                raise ValueError("ambiguous reference repair")
            reference[path] = reference[path].replace(old, new)
        repository = "profile-tools/" + name
    probes = {"controls": s.controls, **s.falsy}
    private = {f"probe_{k}.py": s.setup + "\n" + body for k, body in probes.items()}
    private["grade.py"] = "import runpy\n" + "".join(f"runpy.run_path({k!r})\n" for k in private)
    public = s.setup + "\n" + s.public
    t = Task(id="transfer-" + name, repository=repository, source_group=repository, split="dev",
             issue=s.issue, editable=list(reference), files={**files, "public_test.py":public},
             public_command=["python", "public_test.py"], grader_files=private, grader_command=["python","grade.py"])
    return t, reference
