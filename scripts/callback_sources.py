"""Four executable workspaces for two distinct caller-supplied collaborator contracts."""

from object_revision_cases import workspace


def depot_receipts():
    return workspace(
        "depot-receipts",
        "Recording a handover at an offline depot still asks dispatch for a receipt writer when the driver "
        "has supplied one. Keep the chosen observer for immediate and deferred handovers.",
        "hand_over returns the observer that wrote the receipt. Deferred handovers keep their observer "
        "until confirm runs. A missing observer comes from the dispatch desk, once per acknowledgement.",
        {
            "handover.py": """
            from pending import entry
            def hand_over(parcel, desk, observer=None):
                normal = desk.writer()
                write = observer or normal
                write(parcel)
                return write

            def defer(parcel, observer=None):
                return entry(parcel, observer)
         """,
            "pending.py": """
            def entry(parcel, observer):
                row = {"parcel": parcel}
                if observer:
                    row["ack"] = observer
                return row

            def confirm(row, desk):
                write = row.get("ack") or desk.writer()
                write(row["parcel"])
                return write
         """,
        },
        [
            (
                "handover.py",
                "normal = desk.writer()\n    write = observer or normal",
                "write = desk.writer() if observer is None else observer",
            ),
            ("pending.py", "if observer:", "if observer is not None:"),
            (
                "pending.py",
                'write = row.get("ack") or desk.writer()',
                'write = row.get("ack")\n    if write is None:\n        write = desk.writer()',
            ),
        ],
        """
            from handover import hand_over, defer
            from pending import confirm
            class Desk:
                def __init__(self, online=True):
                    self.online, self.calls, self.notes = online, 0, []
                def writer(self):
                    self.calls += 1
                    if not self.online: raise OSError("depot off grid")
                    return self.notes.append
            class Notebook(list):
                def __call__(self, item):
                    self.append(item)
        """,
        "local = Notebook(['cover'])\ndesk=Desk(False)\nassert hand_over('P3',desk,local) is local\nassert local == ['cover','P3'] and desk.calls == 0",
        "d=Desk()\nhand_over('P',d)\nconfirm(defer('S',None),d)\nassert d.notes==['P','S'] and d.calls==2\n"
        "normal=Notebook(['tag'])\nassert confirm(defer('T',normal),d) is normal and normal==['tag','T']",
        direct="book=Notebook()\nd=Desk()\nassert hand_over('P',d,book) is book and book==['P'] and d.calls==0",
        queued="book=Notebook()\nd=Desk()\nassert confirm(defer('S',book),d) is book and book==['S'] and d.calls==0",
    )


def proof_dispatch():
    return workspace(
        "proof-dispatch",
        "A detached print queue opens its delivery desk even though the job has its own completion callback. "
        "Preserve that callback when a proof is printed immediately or acknowledged after resuming a queue.",
        "ProofQueue.print_one sends one completion and returns its recipient. Tickets made while a job is "
        "paused retain their recipient. acknowledge delivers the same completion using the spooler if none was supplied.",
        {
            "queueing.py": """
            from tickets import Ticket
            class ProofQueue:
                def __init__(self, spool):
                    self.spool = spool

                def print_one(self, title, done=None):
                    remote = self.spool.completions()
                    deliver = done or remote
                    deliver(('printed', title))
                    return deliver

                def pause(self, title, done=None):
                    return Ticket(title, done)
         """,
            "tickets.py": """
            class Ticket:
                def __init__(self, title, recipient):
                    self.title = title
                    self.destinations = [recipient] if recipient else []

                def acknowledge(self, spool):
                    if self.destinations:
                        notify = self.destinations[0]
                    else:
                        notify = spool.completions()
                    notify(('printed', self.title))
                    return notify
         """,
        },
        [
            (
                "queueing.py",
                "remote = self.spool.completions()\n        deliver = done or remote",
                "deliver = self.spool.completions() if done is None else done",
            ),
            (
                "tickets.py",
                "[recipient] if recipient else []",
                "[recipient] if recipient is not None else []",
            ),
        ],
        """
            from queueing import ProofQueue
            class Spool:
                def __init__(self, plugged=True):
                    self.plugged, self.opens, self.log = plugged, 0, []
                def completions(self):
                    self.opens += 1
                    if not self.plugged: raise OSError("no spooler")
                    return self.log.append
            class Subscriber:
                def __init__(self, seen=()):
                    self.seen=list(seen)
                def __len__(self):
                    return len(self.seen)
                def __call__(self, message):
                    self.seen.append(message)
        """,
        "local=Subscriber(['ready'])\ns=Spool(False)\nassert ProofQueue(s).print_one('blue',local) is local\n"
        "assert local.seen == ['ready',('printed','blue')] and s.opens == 0",
        "s=Spool(); q=ProofQueue(s)\nq.print_one('a'); q.pause('b').acknowledge(s)\nassert s.log==[('printed','a'),('printed','b')] and s.opens==2\n"
        "subscriber=Subscriber([1]); assert q.pause('x',subscriber).acknowledge(s) is subscriber and s.opens==2",
        immediate="s=Spool(); q=ProofQueue(s); sub=Subscriber()\nassert q.print_one('one',sub) is sub and sub.seen==[('printed','one')] and s.opens==0",
        after_resume="s=Spool(); q=ProofQueue(s); sub=Subscriber()\nticket=q.pause('two',sub)\nassert sub.seen==[]\nassert ticket.acknowledge(s) is sub and sub.seen==[('printed','two')] and s.opens==0",
    )


def cabinet_order():
    return workspace(
        "cabinet-order",
        "Preparing a shelf manifest at a disconnected site consults the central collation service even when "
        "a local ordering key is supplied. Live sorting and merging pre-sorted drawers share the selected collation.",
        "prepare returns an Order with its key. order.arrange sorts new entries, merge_drawers merges existing "
        "sorted drawers stably with the same collation. Omitted keys use the site's collation.",
        {
            "manifest.py": """
            class Order:
                def __init__(self, key):
                    self.key=key

                def arrange(self, rows):
                    return sorted(rows, key=self.key)

            def prepare(site, key=None):
                collation=site.collation()
                return Order(key or collation)
         """,
            "drawers.py": """
            from heapq import merge

            def merge_drawers(site, drawers, key=None):
                options={}
                if key:
                    options['key']=key
                else:
                    options['key']=site.collation()
                return list(merge(*drawers, **options))
         """,
        },
        [
            (
                "manifest.py",
                "collation=site.collation()\n    return Order(key or collation)",
                "return Order(site.collation() if key is None else key)",
            ),
            ("drawers.py", "if key:", "if key is not None:"),
        ],
        """
            from manifest import prepare
            from drawers import merge_drawers
            class Site:
                def __init__(self,available=True):
                    self.available,self.lookups=available,0
                def collation(self):
                    self.lookups+=1
                    if not self.available: raise OSError("offline shelf")
                    return lambda record: record[0]
            class ByShelf:
                def __init__(self, bookmarks):
                    self.bookmarks=bookmarks
                def __len__(self):
                    return len(self.bookmarks)
                def __call__(self, record):
                    return record[1]
            a=('z',1); b=('a',3)
        """,
        "key=ByShelf(['current']); s=Site(False); order=prepare(s,key)\nassert order.key is key\nassert order.arrange([b,a])==[a,b] and s.lookups==0",
        "s=Site(); assert prepare(s).arrange([a,b])==[b,a]\nassert merge_drawers(s,[[a],[b]],None)==[b,a] and s.lookups==2\n"
        "k=ByShelf([1]); assert merge_drawers(s,[[a],[b]],k)==[a,b] and s.lookups==2",
        new_entries="s=Site(); k=ByShelf([]); order=prepare(s,k)\nassert order.key is k and order.arrange([b,a])==[a,b] and s.lookups==0",
        merged_drawers="s=Site(); k=ByShelf([])\nassert merge_drawers(s,[[a],[b]],k)==[a,b] and s.lookups==0",
    )


def event_collation():
    return workspace(
        "event-collation",
        "Building an offline replay plan requests a schema resolver despite the caller supplying a local "
        "ordering strategy. Keep the selected strategy for both replay planning and inserting late events.",
        "plan orders a batch and returns the strategy with the ordered events. A late event is inserted after "
        "existing events of equal rank. Explicit strategies are callable and may keep their own cache. The "
        "resolver supplies the standard ranking when the caller omits a strategy.",
        {
            "replay.py": """
            from dataclasses import dataclass
            @dataclass
            class Plan:
                strategy: object
                events: list

            def plan(events, resolver, strategy=None):
                built_in = resolver.rank()
                rank = strategy or built_in
                return Plan(rank, sorted(events, key=rank))
         """,
            "late.py": """
            from bisect import bisect_right

            def insert_late(ordered, event, resolver, strategy=None):
                local = {}
                if strategy:
                    local['rank'] = strategy
                rank = local['rank'] if 'rank' in local else resolver.rank()
                positions = [rank(row) for row in ordered]
                at = bisect_right(positions, rank(event))
                return ordered[:at] + [event] + ordered[at:]
         """,
        },
        [
            (
                "replay.py",
                "built_in = resolver.rank()\n    rank = strategy or built_in",
                "rank = resolver.rank() if strategy is None else strategy",
            ),
            ("late.py", "if strategy:", "if strategy is not None:"),
        ],
        """
            from replay import plan
            from late import insert_late
            class Resolver:
                def __init__(self,attached=True):
                    self.attached,self.reads=attached,0
                def rank(self):
                    self.reads+=1
                    if not self.attached: raise RuntimeError("no schema channel")
                    return lambda item:item[0]
            class StationRank(dict):
                def __call__(self, item):
                    return item[1]
            first=('z',1); second=('a',2); late=('m',1)
        """,
        "rank=StationRank(active=True); r=Resolver(False); run=plan([second,first],r,rank)\nassert run.strategy is rank\nassert run.events==[first,second] and r.reads==0",
        "r=Resolver(); assert plan([first,second],r).events==[second,first]\nassert insert_late([second,first],late,r)==[second,late,first] and r.reads==2\n"
        "rank=StationRank(active=True); assert insert_late([first,second],late,r,rank)==[first,late,second] and r.reads==2",
        bulk="r=Resolver(); rank=StationRank(); x=plan([second,first],r,rank)\nassert x.strategy is rank and x.events==[first,second] and r.reads==0",
        late_insert="r=Resolver(); rank=StationRank()\nassert insert_late([first,second],late,r,rank)==[first,late,second] and r.reads==0",
    )


def sources():
    answer = {}
    for family, pilot, confirmation in (
        ("deferred-callback", depot_receipts, proof_dispatch),
        ("caller-collation", cabinet_order, event_collation),
    ):
        stages = {}
        for label, maker in (("pilot", pilot), ("confirmation", confirmation)):
            task, patch = maker()
            name = maker.__name__.replace("_", "-")
            stages[label] = (
                task.model_copy(
                    update={
                        "id": name,
                        "split": "learn" if label == "pilot" else "dev",
                        "source_group": "callback-workspaces/" + name,
                        "repository": "callback-workspaces/" + name,
                    }
                ),
                patch,
            )
        answer[family] = stages
    return answer
