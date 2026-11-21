"""Six independent learn-only workspaces with caller-owned Python collaborators."""

from textwrap import dedent

from evocoder.contracts import Task


def code(s):
    return dedent(s).strip() + "\n"


def workspace(name, issue, contract, files, replacements, setup, public, controls, **boundaries):
    sources = {k: code(v) for k, v in files.items()}
    fixed = dict(sources)
    for path, old, new in replacements:
        if fixed[path].count(old) != 1:
            raise ValueError(f"ambiguous reference edit in {name}/{path}")
        fixed[path] = fixed[path].replace(old, new)
    checks = {"controls": controls, **boundaries}
    hidden = {f"probe_{k}.py": code(setup) + code(v) for k, v in checks.items()}
    hidden["grade.py"] = "import runpy\n" + "".join(f"runpy.run_path({k!r})\n" for k in hidden)
    repo = "python-workflows/" + name
    task = Task(
        id="learn-" + name,
        source_group=repo,
        repository=repo,
        split="learn",
        issue=issue,
        files={**sources, "README.md": contract + "\n", "public_test.py": code(setup) + code(public)},
        editable=list(sources),
        public_command=["python", "public_test.py"],
        grader_files=hidden,
        grader_command=["python", "grade.py"],
    )
    return task, fixed


def tile_compositor():
    return workspace(
        "tile-compositor",
        "Compositing a tile offline still tries to open the plugin registry even when "
        "the caller passed its own blending operation. Preserve custom blending in regular and blank tiles.",
        "Compositor accepts a binary blend callable. Each pixel uses that instance. Missing operations "
        "are resolved by the registry when a tile is actually rendered. An empty tile has one padding pixel.",
        {
            "tiles.py": """
            class Compositor:
                def __init__(self, registry, blend=None):
                    self.registry = registry
                    self.blend = blend

                def tile(self, pairs):
                    if not pairs:
                        return self._padding()
                    operation = self.registry.resolve()
                    if self.blend:
                        operation = self.blend
                    return [operation(a, b) for a, b in pairs]

                def _padding(self):
                    operation = self.blend or self.registry.resolve()
                    return [operation(0, 0)]
        """
        },
        [
            (
                "tiles.py",
                "operation = self.registry.resolve()\n        if self.blend:\n            operation = self.blend",
                "operation = self.registry.resolve() if self.blend is None else self.blend",
            ),
            (
                "tiles.py",
                "self.blend or self.registry.resolve()",
                "self.registry.resolve() if self.blend is None else self.blend",
            ),
        ],
        """
            from tiles import Compositor
            class Registry:
                def __init__(self, offline=False):
                    self.offline, self.lookups = offline, 0
                def resolve(self):
                    self.lookups += 1
                    if self.offline:
                        raise RuntimeError('registry offline')
                    return max
            class Brush:
                def __init__(self):
                    self.samples = []
                def __len__(self):
                    return len(self.samples)
                def __call__(self, a, b):
                    self.samples.append((a,b))
                    return a + b + 1
        """,
        """
            r = Registry(True)
            brush = Brush()
            assert Compositor(r, brush).tile([(1,2)]) == [4]
            assert brush.samples == [(1,2)] and r.lookups == 0
        """,
        """
            r = Registry()
            c = Compositor(r)
            assert c.tile([(1,3)]) == [3] and c.tile([]) == [0]
            assert r.lookups == 2
            assert Compositor(r, min).tile([(1,3)]) == [1]
        """,
        initially_empty_brush="""
            b, r = Brush(), Registry()
            assert Compositor(r,b).tile([(3,4)]) == [8] and r.lookups == 0
            assert b.samples == [(3,4)]
        """,
        padding_first="""
            b, r = Brush(), Registry(True)
            assert Compositor(r,b).tile([]) == [1]
            assert b.samples == [(0,0)] and r.lookups == 0
        """,
    )


def scheduler_watch():
    return workspace(
        "scheduler-watch",
        "Starting a scheduled watch with a simulated clock reads system time before "
        "the simulation has started. Resuming a paused ticket should keep its selected time source.",
        "Watch stores its optional clock. Start and wake query that clock for timestamps. When absent, "
        "each operation obtains the host clock from the node. Pausing and restoring a ticket preserves its clock.",
        {
            "watch.py": """
            class Watch:
                def __init__(self, node, clock=None):
                    self.node, self.clock = node, clock

                def start(self):
                    active = self.node.clock()
                    if self.clock is not None:
                        active = self.clock
                    return (active.now(), self.clock)

                def wake(self, ticket):
                    from ticket import select
                    return select(self.node, ticket).now()
        """,
            "ticket.py": """
            def select(node, ticket):
                _, saved = ticket
                return next(filter(None, (saved,)), None) or node.clock()
        """,
        },
        [
            (
                "watch.py",
                "active = self.node.clock()\n        if self.clock is not None:\n            active = self.clock",
                "active = self.node.clock() if self.clock is None else self.clock",
            ),
            (
                "ticket.py",
                "next(filter(None, (saved,)), None) or node.clock()",
                "node.clock() if saved is None else saved",
            ),
        ],
        """
            from watch import Watch
            class Clock:
                def __init__(self, time): self.time = time
                def now(self): return self.time
            class Replay(Clock):
                def __bool__(self): return False
            class Node:
                def __init__(self, offline=False): self.offline, self.reads = offline, 0
                def clock(self):
                    self.reads += 1
                    if self.offline: raise RuntimeError('system clock unavailable')
                    return Clock(42)
        """,
        """
            r = Clock(1)
            assert Watch(Node(True),r).start() == (1,r)
        """,
        """
            n = Node()
            w = Watch(n)
            assert w.wake(w.start()) == 42 and n.reads == 2
            c = Clock(7)
            assert Watch(n,c).wake((0,c)) == 7
        """,
        start_replay="""
            n, c = Node(True), Replay(0)
            t = Watch(n,c).start()
            assert t[0] == 0 and t[1] is c and n.reads == 0
        """,
        wake_replay="""
            n, c = Node(True), Replay(0)
            assert Watch(n).wake((19,c)) == 0 and n.reads == 0
        """,
    )


def undo_journal():
    return workspace(
        "undo-journal",
        "Undo groups can detach from a caller's journal on the first change. "
        "Nested groups and abandoned changes must remain attached to the originating journal.",
        "Document.begin accepts an optional journal. For all nested handles, record and rollback affect "
        "that supplied object and close returns it. Separate top-level groups without a journal are independent.",
        {
            "document.py": """
            from changes import Group

            def begin(journal=None):
                return Group(journal or [])
        """,
            "changes.py": """
            class Group:
                def __init__(self, journal):
                    self.journal = journal
                    self.start = len(journal)

                def nested(self):
                    return Group(self.journal if len(self.journal) else [])

                def record(self, text):
                    self.journal.append(text)

                def close(self, abort=False):
                    if abort:
                        del self.journal[self.start:]
                    return self.journal
        """,
        },
        [
            ("document.py", "journal or []", "[] if journal is None else journal"),
            ("changes.py", "self.journal if len(self.journal) else []", "self.journal"),
        ],
        "from document import begin",
        """
            journal = []
            g = begin(journal)
            g.record('insert')
            assert g.close() is journal and journal == ['insert']
        """,
        """
            a, b = begin(), begin()
            a.record('a')
            assert a.close() == ['a'] and not b.close()
            journal = ['old']
            g = begin(journal).nested()
            g.record('new')
            assert g.close(True) is journal and journal == ['old']
        """,
        nested_before_record="""
            journal = []
            g = begin(journal).nested()
            g.record('first')
            assert g.close() is journal and journal == ['first']
        """,
        abort_empty_origin="""
            class Journal(list):
                def __bool__(self): return False
            journal = Journal(['saved'])
            g = begin(journal)
            g.record('temporary')
            assert journal == ['saved','temporary']
            assert g.close(True) is journal and journal == ['saved']
        """,
    )


def stream_framing():
    return workspace(
        "stream-framing",
        "Attaching a decoder to a binary reader can allocate an unwanted default codec. "
        "Continuation reads must go through the same decoder, including when it has no buffered bytes.",
        "Reader.feed and Reader.finish use the supplied stateful decoder for every chunk. An external "
        "decoder is shared by identity. Without one, one private codec is allocated lazily per reader.",
        {
            "reader.py": """
            class Reader:
                def __init__(self, factory, codec=None):
                    self.factory = factory
                    self.external = codec
                    self.local = None

                def _private(self):
                    if self.local is None:
                        self.local = self.factory()
                    return self.local

                def feed(self, chunk):
                    decoder = self._private()
                    if self.external is not None:
                        decoder = self.external
                    return decoder.accept(chunk)

                def finish(self):
                    decoder = self.external or self._private()
                    return decoder.accept(b'')
        """
        },
        [
            (
                "reader.py",
                "decoder = self._private()\n        if self.external is not None:\n            decoder = self.external",
                "decoder = self._private() if self.external is None else self.external",
            ),
            (
                "reader.py",
                "self.external or self._private()",
                "self._private() if self.external is None else self.external",
            ),
        ],
        """
            from reader import Reader
            class Codec:
                def __init__(self): self.chunks = []
                def accept(self, chunk): self.chunks.append(chunk); return tuple(self.chunks)
            class Idle(Codec):
                def __len__(self): return 0
            def disabled(): raise RuntimeError('no installed codec')
        """,
        """
            codec = Codec()
            assert Reader(disabled,codec).feed(b'x') == (b'x',)
        """,
        """
            made = []
            def factory():
                c = Codec(); made.append(c); return c
            a = Reader(factory)
            assert not made
            assert a.feed(b'a') == (b'a',) and a.finish() == (b'a',b'') and len(made) == 1
            assert Reader(factory).finish() == (b'',) and len(made) == 2
        """,
        drain_idle="""
            x = Idle()
            assert Reader(disabled,x).finish() == (b'',) and x.chunks == [b'']
        """,
        drain_after_data="""
            x = Idle()
            r = Reader(Codec,x)
            assert r.feed(b'1') == (b'1',)
            assert r.finish() == (b'1',b'') and x.chunks == [b'1',b'']
        """,
    )


def build_walk():
    return workspace(
        "incremental-walk",
        "Incremental dependency walks sometimes rebuild visited targets. A caller can "
        "share its visit registry across multiple roots and across invocations of the planner.",
        "plan and forest return new visit order. Both update the supplied mutable set, even when empty. "
        "Supplying no set starts a fresh walk. Previously visited nodes stop recursion.",
        {
            "walk.py": """
            def plan(graph, target, visited=None):
                marks = visited or set()
                out = []
                def visit(item):
                    if item in marks:
                        return
                    marks.add(item)
                    out.append(item)
                    for dep in graph.get(item, ()):
                        visit(dep)
                visit(target)
                return out
        """,
            "forest.py": """
            from walk import plan

            def forest(graph, roots, visited=None):
                shared = visited if visited else set()
                order = []
                for root in roots:
                    order.extend(plan(graph, root, shared))
                return order
        """,
        },
        [
            ("walk.py", "visited or set()", "set() if visited is None else visited"),
            ("forest.py", "visited if visited else set()", "set() if visited is None else visited"),
        ],
        """
            from walk import plan
            from forest import forest
            graph = {'app':['lib'],'tool':['lib'],'lib':[]}
        """,
        """
            seen = set()
            assert plan(graph,'app',seen) == ['app','lib']
            assert seen == {'app','lib'}
        """,
        """
            assert plan(graph,'app') == plan(graph,'app') == ['app','lib']
            existing = {'lib'}
            assert forest(graph,['app','tool'],existing) == ['app','tool']
            assert existing == {'app','lib','tool'}
        """,
        shared_empty_forest="""
            seen = set()
            assert forest(graph,['app','tool'],seen) == ['app','lib','tool']
            assert seen == {'app','lib','tool'} and plan(graph,'tool',seen) == []
        """,
        truth_independent="""
            class Registry(set):
                def __bool__(self): raise AssertionError('visitation must use membership')
            seen = Registry(['lib'])
            assert forest(graph,['tool'],seen) == ['tool'] and 'tool' in seen
        """,
    )


def lease_scope():
    return workspace(
        "subscription-lease",
        "Delivering a notification under a borrowed lease still asks the service to open a "
        "lease. Re-delivery of a deferred notice must re-enter its original lease and release it exactly once.",
        "A service opens a default context manager only when no lease is supplied. The manager and its entered "
        "resource are distinct. Defer transports the manager itself and redeliver re-enters it.",
        {
            "deliver.py": """
            def send(service, message, lease=None):
                manager = service.open()
                if lease:
                    manager = lease
                with manager as channel:
                    return channel(message)

            def defer(message, lease=None):
                return {'message': message, 'manager': lease}
        """,
            "retry.py": """
            from deliver import send

            def redeliver(service, pending):
                manager = pending.get('manager')
                if not manager:
                    manager = None
                return send(service, pending['message'], manager)
        """,
        },
        [
            (
                "deliver.py",
                "manager = service.open()\n    if lease:\n        manager = lease",
                "manager = service.open() if lease is None else lease",
            ),
            ("retry.py", "    if not manager:\n        manager = None\n", ""),
        ],
        """
            from deliver import send, defer
            from retry import redeliver
            class Lease:
                def __init__(self, prefix): self.prefix, self.events = prefix, []
                def __enter__(self): self.events.append('enter'); return lambda m:self.prefix+m
                def __exit__(self,*args): self.events.append('exit')
            class Borrowed(Lease):
                def __bool__(self): return False
            class Service:
                def __init__(self, offline=False): self.offline,self.opens=offline,0
                def open(self):
                    self.opens += 1
                    if self.offline: raise RuntimeError('detached subscription')
                    return Lease('owned:')
        """,
        """
            b = Lease('borrowed:')
            assert send(Service(True),'a',b) == 'borrowed:a'
            assert b.events == ['enter','exit']
        """,
        """
            s = Service()
            assert send(s,'a') == 'owned:a'
            assert redeliver(s,defer('b')) == 'owned:b' and s.opens == 2
            b = Lease('manual:')
            assert redeliver(s,defer('x',b)) == 'manual:x'
        """,
        direct_borrow="""
            b, s = Borrowed('b:'), Service(True)
            assert send(s,'one',b) == 'b:one' and s.opens == 0 and b.events == ['enter','exit']
        """,
        deferred_borrow="""
            b, s = Borrowed('b:'), Service()
            ticket = defer('two',b)
            assert ticket['manager'] is b
            assert redeliver(s,ticket) == 'b:two' and b.events == ['enter','exit'] and s.opens == 0
        """,
    )


def cases():
    return [
        tile_compositor(),
        scheduler_watch(),
        undo_journal(),
        stream_framing(),
        build_walk(),
        lease_scope(),
    ]
