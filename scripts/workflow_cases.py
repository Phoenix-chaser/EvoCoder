"""Independent learning programs and two predeclared, independently implemented callback sources."""

from textwrap import dedent

from object_revision_cases import workspace


def example(name, issue, contract, files, fixes, setup, public, controls, *, kind, boundaries, feedback=None):
    task, ref = workspace(name, issue, contract, files, fixes, setup, public, controls, **boundaries)
    task = task.model_copy(
        update={
            "id": name,
            "repository": "workflow-contracts/" + name,
            "source_group": "workflow-contracts/" + name,
            "split": "learn" if kind in ("flag", "wake") else "dev",
        }
    )
    # Feedback is a separate public-contract exercise for the learner, never a view of a grader file.
    program = None if feedback is None else dedent(setup).strip() + "\n" + dedent(feedback).strip() + "\n"
    return task, ref, program


def badge_alerts():
    return example(
        "badge-alerts",
        "Offline badge kiosks still open the tenant preferences even when the caller supplied "
        "its own alert choice. Keep explicit choices when an alert preference is saved and opened again.",
        "alert_choice returns a boolean. A JSON profile can contain true, false or null. Missing and null inherit the "
        "tenant preference lazily. save_profile preserves explicit choices and does not freeze an inherited tenant setting.",
        {
            "badges.py": """
        def alert_choice(profile, tenant):
            fallback = tenant.alerts()
            return profile.get('alerts', fallback)

        def save_profile(profile):
            return {'choices': {k:v for k,v in profile.items() if v}}

        def reopen(saved, tenant):
            return alert_choice(saved['choices'], tenant)
      """
        },
        [
            (
                "badges.py",
                "fallback = tenant.alerts()\n    return profile.get('alerts', fallback)",
                "choice = profile.get('alerts')\n    return tenant.alerts() if choice is None else choice",
            ),
            ("badges.py", "if v}", "if v is not None}"),
        ],
        """
        from badges import alert_choice, save_profile, reopen
        class Tenant:
            def __init__(self, value=True, offline=False):
                self.value,self.offline,self.reads=value,offline,0
            def alerts(self):
                self.reads+=1
                if self.offline: raise LookupError('no tenant connection')
                return self.value
      """,
        "t=Tenant(offline=True)\nassert alert_choice({'alerts':False}, t) is False\nassert t.reads==0",
        "t=Tenant(False)\nassert alert_choice({},t) is False and alert_choice({'alerts':None},t) is False and t.reads==2\nassert alert_choice({'alerts':True},t) is True and t.reads==2",
        kind="flag",
        boundaries={
            "stored_false": "t=Tenant(offline=True)\nassert reopen(save_profile({'alerts':False}),t) is False and t.reads==0",
            "inherited": "s=save_profile({'alerts':None}); t=Tenant(True)\nassert reopen(s,t) is True\nt.value=False\nassert reopen(s,t) is False and t.reads==2",
        },
        feedback="s=save_profile({'alerts':False}); t=Tenant(True)\nassert reopen(s,t) is False and t.reads==0\ns=save_profile({})\nassert reopen(s,t) is True and t.reads==1",
    )


def preview_toolbar():
    return example(
        "preview-toolbar",
        "A desktop preview still requests a remote layout even when the caller passes "
        "its own wrap setting. Reopening a saved draft must keep that choice.",
        "wrap_for returns the boolean wrap preference. A named local value wins, including False. Missing or null inherits "
        "the current style. Drafts store the local preference so a future style update can still reach an inherited draft.",
        {
            "layout.py": """
        def wrap_for(style, prefs):
            default = style['wrap']
            return prefs.get('wrap') or default
      """,
            "draft.py": """
        from layout import wrap_for
        def checkpoint(prefs):
            return tuple((k,v) for k,v in prefs.items() if v)
        def resume(draft, style):
            return wrap_for(style,dict(draft))
      """,
        },
        [
            (
                "layout.py",
                "default = style['wrap']\n    return prefs.get('wrap') or default",
                "v = prefs.get('wrap')\n    return style['wrap'] if v is None else v",
            ),
            ("draft.py", "if v)", "if v is not None)"),
        ],
        "from layout import wrap_for\nfrom draft import checkpoint,resume",
        "assert wrap_for({}, {'wrap':True}) is True",
        "assert wrap_for({'wrap':True},{'wrap':None}) is True\nassert wrap_for({'wrap':False},{}) is False\nassert resume(checkpoint({'wrap':True}),{}) is True",
        kind="flag",
        boundaries={
            "direct_false": "assert wrap_for({}, {'wrap':False}) is False",
            "restart_false": "assert resume(checkpoint({'wrap':False}),{'wrap':True}) is False",
        },
        feedback="prefs={'wrap':False}\nassert wrap_for({'wrap':True},prefs) is False\nassert resume(checkpoint(prefs),{}) is False\nassert resume(checkpoint({'wrap':None}),{'wrap':True}) is True",
    )


def harbor_slots():
    return example(
        "harbor-slots",
        "Opening a berth reservation tries to allocate a station scanner despite an inspector "
        "being supplied. After a tide pause, resume the reservation using the inspector that the ticket carries.",
        "begin returns a ticket with the original optional inspector and its reading. resume may be called on a fresh "
        "desk. The ticket owns the selection. With no inspector, each reading uses that desk's current station scanner once.",
        {
            "berth.py": """
        from manifest import packed, unpack
        class Desk:
            def __init__(self, station, inspector=None):
                self.station,self.inspector=station,inspector
            def begin(self):
                default=self.station.acquire()
                reader=self.inspector or default
                return packed(reader.read(),self.inspector)
            def resume(self,ticket):
                last,reader=unpack(ticket)
                chosen=reader or self.inspector or self.station.acquire()
                return (last,chosen.read())
      """,
            "manifest.py": """
        def packed(last, inspector):
            fields=[('last',last)]
            if inspector:
                fields.append(('owner',inspector))
            return tuple(fields)
        def unpack(ticket):
            fields=dict(ticket)
            readers=[v for k,v in ticket if k=='owner' and v]
            return fields['last'],next(iter(readers),None)
      """,
        },
        [
            (
                "berth.py",
                "default=self.station.acquire()\n        reader=self.inspector or default",
                "reader=self.station.acquire() if self.inspector is None else self.inspector",
            ),
            (
                "berth.py",
                "reader or self.inspector or self.station.acquire()",
                "self.station.acquire() if reader is None else reader",
            ),
            ("manifest.py", "if inspector:", "if inspector is not None:"),
            ("manifest.py", "if k=='owner' and v", "if k=='owner'"),
        ],
        """
        from berth import Desk
        class Gauge:
            def __init__(self,n): self.n=n
            def read(self): self.n+=1; return self.n
        class Standby(Gauge):
            def __bool__(self): return False
        class Station:
            def __init__(self,offline=False): self.offline,self.opens=offline,0
            def acquire(self):
                self.opens+=1
                if self.offline: raise LookupError('station unavailable')
                return Gauge(70)
      """,
        "g=Gauge(6); t=Desk(Station(True),g).begin()\nassert dict(t)['owner'] is g\nassert dict(t)['last']==7",
        "s=Station(); d=Desk(s); t=d.begin(); assert d.resume(t)==(71,71) and s.opens==2\ng=Gauge(0); t=Desk(s,g).begin(); assert Desk(s,Gauge(7)).resume(t)==(1,2) and s.opens==2",
        kind="wake",
        boundaries={
            "saved_owner": "s=Station(True); g=Standby(4); t=Desk(s,g).begin(); assert dict(t)['owner'] is g and s.opens==0",
            "resumed": "s=Station(True); g=Standby(40); t=Desk(s,g).begin(); assert Desk(s,Gauge(2)).resume(t)==(41,42) and s.opens==0",
            "absent_after_move": "s=Station(); t=Desk(s).begin(); other=Station(); assert Desk(other,Gauge(2)).resume(t)==(71,71) and other.opens==1",
        },
        feedback="g=Standby(9); station=Station(); ticket=Desk(station,g).begin()\nassert Desk(station,Gauge(30)).resume(ticket)==(10,11) and station.opens==0\nplain=Station(); t=Desk(plain).begin(); assert Desk(plain,Gauge(3)).resume(t)==(71,71) and plain.opens==2",
    )


def mixer_restart():
    return example(
        "mixer-restart",
        "A render session still connects to the studio even with a supplied level meter. A "
        "different worker can wake a suspended channel. It must use the suspended channel's selected meter.",
        "sample samples a meter and suspends the channel. wake takes a ticket and a new studio, not the original channel. "
        "A supplied meter stays live and sees both labels. An omitted meter is looked up afresh for every sample.",
        {
            "channel.py": """
        import capsule
        def sample(studio, meter=None):
            house = studio.meter()
            active = meter if meter else house
            return capsule.encode(active.measure('start'), meter)
        def wake(ticket, studio):
            v, chain = capsule.decode(ticket)
            active = next(filter(None,chain),None) or studio.meter()
            return v, active.measure('wake')
      """,
            "capsule.py": """
        def encode(value, meter):
            return {'reading':value, 'meters':tuple(x for x in [meter] if x)}
        def decode(ticket):
            return ticket['reading'],ticket.get('meters',())
      """,
        },
        [
            (
                "channel.py",
                "house = studio.meter()\n    active = meter if meter else house",
                "active = studio.meter() if meter is None else meter",
            ),
            ("capsule.py", "if x)", "if x is not None)"),
            (
                "channel.py",
                "active = next(filter(None,chain),None) or studio.meter()",
                "active = next((x for x in chain if x is not None), None)\n    if active is None:\n        active = studio.meter()",
            ),
        ],
        """
        from channel import sample,wake
        class Meter:
            def __init__(self,n): self.n,self.seen=n,[]
            def measure(self,label):
                self.seen.append(label)
                return self.n+len(self.seen)
        class Muted(Meter):
            def __len__(self): return 0
        class Studio:
            def __init__(self,online=True): self.online,self.calls=online,0
            def meter(self):
                self.calls+=1
                if not self.online: raise LookupError('studio offline')
                return Meter(80)
      """,
        "m=Meter(6); t=sample(Studio(False),m)\nassert t['meters'][0] is m\nassert t['reading']==7",
        "s=Studio(); t=sample(s); assert wake(t,s)==(81,81) and s.calls==2\nm=Meter(4); t=sample(s,m); assert wake(t,s)==(5,6) and s.calls==2",
        kind="wake",
        boundaries={
            "packed": "m=Muted(7); s=Studio(False); t=sample(s,m); assert t['meters']==(m,) and m.seen==['start'] and s.calls==0",
            "resumed": "m=Muted(7); s=Studio(False); t=sample(s,m); assert wake(t,Studio(False))==(8,9) and m.seen==['start','wake']",
        },
        feedback="m=Muted(12); first=Studio(); second=Studio(); t=sample(first,m)\nassert wake(t,second)==(13,14) and m.seen==['start','wake'] and first.calls+second.calls==0\ns=Studio(); assert wake(sample(s),s)==(81,81) and s.calls==2",
    )


def journal_cursor():
    return example(
        "journal-cursor",
        "An offline journal reader contacts its progress service even when a caller supplies a "
        "checkpoint listener. Keep that listener on a page advance, an empty poll, and when the saved cursor is committed.",
        "poll returns a cursor and notifies its listener of the high-water mark. Empty polls report idle. commit notifies "
        "the same listener of the mark. If unspecified, each notification resolves the service's current listener once.",
        {
            "journal.py": """
        from offsets import bookmark, idle
        def poll(rows, service, listener=None):
            if not rows:
                return idle(service,listener)
            normal=service['progress']
            tick=listener or normal
            mark=max(rows)
            tick(('advance',mark))
            return bookmark(mark,listener)
      """,
            "offsets.py": """
        def bookmark(mark, sink):
            return (mark, tuple(x for x in [sink] if x))
        def idle(service,sink):
            tick=next(filter(None,[sink]),None) or service['progress']
            tick(('idle',0))
            return bookmark(0,sink)
        def commit(cursor,service):
            mark,slot=cursor
            tick=(slot[0] if slot else None) or service['progress']
            tick(('commit',mark))
            return tick
      """,
        },
        [
            (
                "journal.py",
                "normal=service['progress']\n    tick=listener or normal",
                "tick=service['progress'] if listener is None else listener",
            ),
            ("offsets.py", "if x)", "if x is not None)"),
            (
                "offsets.py",
                "next(filter(None,[sink]),None) or service['progress']",
                "service['progress'] if sink is None else sink",
            ),
            (
                "offsets.py",
                "(slot[0] if slot else None) or service['progress']",
                "slot[0] if slot else service['progress']",
            ),
        ],
        """
        from journal import poll
        from offsets import commit
        class Listener:
            def __init__(self): self.log=[]
            def __call__(self,msg): self.log.append(msg)
      """,
        "watch=Listener(); token=poll([5,4],{},watch)\nassert token[1][0] is watch\nassert watch.log==[('advance',5)]",
        "w=Listener(); srv={'progress':w}; commit(poll([2],srv),srv); commit(poll([],srv),srv); assert w.log==[('advance',2),('commit',2),('idle',0),('commit',0)]\na=Listener(); assert commit(poll([],srv,a),srv) is a",
        kind="screen",
        boundaries={
            "stored": "w=Listener(); type(w).__bool__=lambda self:False\nt=poll([9],{},w); assert commit(t,{}) is w and w.log==[('advance',9),('commit',9)]",
            "idle": "w=Listener(); type(w).__bool__=lambda self:False\nt=poll([],{},w); assert commit(t,{}) is w and w.log==[('idle',0),('commit',0)]",
        },
    )


def watermark_bus():
    return example(
        "watermark-bus",
        "A disconnected replication follower still opens the broker for checkpoint delivery even "
        "when the caller installed a progress observer. Preserve the observer across an empty heartbeat and delayed acknowledgement.",
        "Follower.advance reports the latest sequence (zero for a heartbeat). It returns an acknowledgement carrying the "
        "optional observer for later delivery on any broker. Without an observer, resolve the current broker for each delivery.",
        {
            "replica.py": """
        from delivery import Ack, heartbeat
        class Follower:
            def __init__(self,broker): self.broker=broker
            def advance(self,seqs,observer=None):
                if not seqs:
                    return heartbeat(observer,self.broker)
                dispatch=self.broker.open_progress()
                cb=observer or dispatch
                cb('advance',seqs[-1])
                return Ack(seqs[-1],observer)
      """,
            "delivery.py": """
        class Ack:
            def __init__(self,seq,observer):
                self.seq=seq
                self.routes=dict([('local',observer)] if observer else [])
            def finish(self, broker):
                local=self.routes.get('local')
                cb=next((x for x in (local,) if x),None) or broker.open_progress()
                cb('ack',self.seq)
                return cb
        def heartbeat(listener,broker):
            if listener: cb=listener
            else: cb=broker.open_progress()
            cb('idle',0)
            return Ack(0,listener)
      """,
        },
        [
            (
                "replica.py",
                "dispatch=self.broker.open_progress()\n        cb=observer or dispatch",
                "cb=self.broker.open_progress() if observer is None else observer",
            ),
            ("delivery.py", "if observer else []", "if observer is not None else []"),
            (
                "delivery.py",
                "next((x for x in (local,) if x),None) or broker.open_progress()",
                "broker.open_progress() if local is None else local",
            ),
            ("delivery.py", "if listener:", "if listener is not None:"),
        ],
        """
        from replica import Follower
        class Observer:
            def __init__(self): self.events=[]
            def __call__(self,event,seq): self.events.append((event,seq))
        class Broker:
            def __init__(self,live=True): self.live,self.calls,self.default=live,0,Observer()
            def open_progress(self):
                self.calls+=1
                if not self.live: raise LookupError('broker detached')
                return self.default
      """,
        "local=Observer(); b=Broker(False); t=Follower(b).advance([2,7],local)\nassert t.routes['local'] is local\nassert local.events==[('advance',7)] and b.calls==0",
        "b=Broker(); f=Follower(b); f.advance([4]).finish(b); f.advance([]).finish(b); assert b.calls==4 and b.default.events==[('advance',4),('ack',4),('idle',0),('ack',0)]\nw=Observer(); assert f.advance([],w).finish(b) is w and b.calls==4",
        kind="screen",
        boundaries={
            "delayed": "w=Observer(); type(w).__len__=lambda self:0\nb=Broker(False); t=Follower(b).advance([6],w); assert t.finish(Broker(False)) is w and w.events==[('advance',6),('ack',6)]",
            "heartbeat": "w=Observer(); type(w).__len__=lambda self:0\nb=Broker(False); t=Follower(b).advance([],w); assert t.finish(Broker(False)) is w and w.events==[('idle',0),('ack',0)]",
        },
    )


def cases():
    return {
        maker.__name__.replace("_", "-"): maker()
        for maker in (
            badge_alerts,
            preview_toolbar,
            harbor_slots,
            mixer_restart,
            journal_cursor,
            watermark_bus,
            rollback_batch,
            compensation_stack,
        )
    }


def rollback_batch():
    return example(
        "rollback-batch",
        "Staging work in an isolated transaction accesses the audit host although the client "
        "provided its rollback handler. Explicit handlers must receive both an abandoned empty batch and a later cancellation.",
        "stage registers one rollback handler. An empty batch immediately notifies it with 'empty'. cancel returns the handler "
        "after notifying it with the saved work. Missing handlers use the current audit service at delivery time.",
        {
            "unit.py": """
        from rollback import Pending, abandon
        def stage(jobs,audit,undo=None):
            if not jobs: return abandon(audit,undo)
            ordinary=audit['undo']
            handler=undo or ordinary
            return Pending(tuple(jobs),handler if undo else None)
      """,
            "rollback.py": """
        class Pending:
            def __init__(self,items,handler):
                self.items=items
                self.callbacks=[handler] if handler else []
            def cancel(self,audit):
                run=next(filter(None,self.callbacks),None) or audit['undo']
                run(self.items)
                return run
        def abandon(audit,run):
            deliver=run or audit['undo']
            deliver('empty')
            return Pending((),run)
      """,
        },
        [
            (
                "unit.py",
                "ordinary=audit['undo']\n    handler=undo or ordinary\n    return Pending(tuple(jobs),handler if undo else None)",
                "return Pending(tuple(jobs),undo)",
            ),
            ("rollback.py", "if handler else []", "if handler is not None else []"),
            (
                "rollback.py",
                "next(filter(None,self.callbacks),None) or audit['undo']",
                "self.callbacks[0] if self.callbacks else audit['undo']",
            ),
            ("rollback.py", "run or audit['undo']", "audit['undo'] if run is None else run"),
        ],
        """
        from unit import stage
        class Hook:
            def __init__(self): self.calls=[]
            def __call__(self,x): self.calls.append(x)
      """,
        "h=Hook(); t=stage([1],{},h); assert t.callbacks[0] is h",
        "h=Hook(); a={'undo':h}; t=stage([3],a); assert t.cancel(a) is h and h.calls==[(3,)]\nw=Hook(); assert stage([],a,w).cancel(a) is w and w.calls==['empty',()]",
        kind="screen",
        boundaries={
            "cancelled": "h=Hook(); type(h).__bool__=lambda self:False\nt=stage([8],{},h); assert t.cancel({}) is h and h.calls==[(8,)]",
            "empty": "h=Hook(); type(h).__bool__=lambda self:False\nt=stage([],{},h); assert t.cancel({}) is h and h.calls==['empty',()]",
        },
    )


def compensation_stack():
    return example(
        "compensation-stack",
        "Preparing a local migration requires the server's compensation sink even when its "
        "caller passed a handler. Carry the same handler from preparation to abort, including a migration with no changes.",
        "prepare returns a token and does not contact the server on nonempty work. With no changes it notifies "
        "the handler with 'noop'. abort delivers the token's changes using the supplied handler or the then-current server sink.",
        {
            "migrate.py": """
        from tokens import encode, no_changes
        def prepare(server,changes,handler=None):
            if not changes: return no_changes(server,handler)
            fallback=server.sink()
            chosen=handler or fallback
            return encode(changes, chosen if handler else None)
      """,
            "tokens.py": """
        def encode(changes, fn):
            envelope={'changes':tuple(changes)}
            if fn: envelope['compensate']=fn
            return envelope
        def no_changes(server, fn):
            chosen=next(filter(None,(fn,)),None) or server.sink()
            chosen('noop')
            return encode([],fn)
        def abort(server, token):
            routes=[v for k,v in token.items() if k=='compensate' and v]
            notify=routes[0] if routes else server.sink()
            notify(token['changes'])
            return notify
      """,
        },
        [
            (
                "migrate.py",
                "fallback=server.sink()\n    chosen=handler or fallback\n    return encode(changes, chosen if handler else None)",
                "return encode(changes,handler)",
            ),
            ("tokens.py", "if fn:", "if fn is not None:"),
            (
                "tokens.py",
                "next(filter(None,(fn,)),None) or server.sink()",
                "server.sink() if fn is None else fn",
            ),
            ("tokens.py", "if k=='compensate' and v", "if k=='compensate'"),
        ],
        """
        from migrate import prepare
        from tokens import abort
        class Sink:
            def __init__(self): self.entries=[]
            def __call__(self,x): self.entries.append(x)
        class Server:
            def __init__(self,enabled=True): self.enabled,self.reads,self.log=enabled,0,Sink()
            def sink(self):
                self.reads+=1
                if not self.enabled: raise LookupError('no connection')
                return self.log
      """,
        "s=Server(False); fn=Sink(); t=prepare(s,['up'],fn)\nassert t['compensate'] is fn\nassert s.reads==0",
        "s=Server(); t=prepare(s,['u']); abort(s,t); assert s.log.entries==[('u',)]\nw=Sink(); t=prepare(s,[],w); assert abort(s,t) is w and w.entries==['noop',()]",
        kind="screen",
        boundaries={
            "rollback": "s=Server(False); w=Sink(); type(w).__len__=lambda self:0\nt=prepare(s,['db'],w); assert abort(s,t) is w and w.entries==[('db',)]",
            "noop": "s=Server(False); w=Sink(); type(w).__len__=lambda self:0\nt=prepare(s,[],w); assert abort(s,t) is w and w.entries==['noop',()]",
        },
    )
