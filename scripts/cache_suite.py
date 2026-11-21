"""Manually authored cache-contract pilot: shared domain, distinct implementations.

These are self-built tasks, not natural repository issues or independent statistical evidence.
Reference implementations are offline controller assets and never enter solver snapshots.
"""
from __future__ import annotations

import textwrap


def code(value):
    return textwrap.dedent(value).strip() + "\n"


HELPERS = code('''
import unittest, threading, asyncio

def launch(fn):
    result = []
    def work():
        try: result.append((True, fn()))
        except BaseException as exc: result.append((False, exc))
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, result

def finish(job):
    thread, result = job
    thread.join(2)
    assert not thread.is_alive(), 'worker did not finish'
    assert result, 'worker had no result'
    if not result[0][0]: raise result[0][1]
    return result[0][1]
''')

SYNC_FIXED = code('''
import threading

class Cache:
    def __init__(self, loader):
        self.loader = loader
        self.values, self.epochs, self.pending = {}, {}, {}
        self.lock = threading.Lock()

    def invalidate(self, key):
        with self.lock:
            self.epochs[key] = self.epochs.get(key, 0) + 1
            self.values.pop(key, None)
            old = self.pending.pop(key, None)
            if old: old[1].set()

    def get(self, key):
        while True:
            with self.lock:
                if key in self.values: return self.values[key]
                epoch = self.epochs.get(key, 0)
                job = self.pending.get(key)
                owner = job is None
                if owner:
                    job = (epoch, threading.Event(), threading.get_ident())
                    self.pending[key] = job
                elif job[2] == threading.get_ident():
                    raise RuntimeError('recursive load of same key')
            if not owner:
                job[1].wait()
                continue
            try:
                value = self.loader(key)
            except BaseException:
                with self.lock:
                    if self.pending.get(key) is job: self.pending.pop(key)
                    job[1].set()
                raise
            with self.lock:
                if self.epochs.get(key, 0) == epoch:
                    self.values[key] = value
                    if self.pending.get(key) is job: self.pending.pop(key)
                    job[1].set()
                    return value
                if self.pending.get(key) is job: self.pending.pop(key)
                job[1].set()
''')

SYNC_PUBLIC = code('''
from cache import Cache
class Contract(unittest.TestCase):
    def test_invalidation_during_load(self):
        entered, release = threading.Event(), threading.Event()
        state = [1]; calls = []
        def loader(key):
            value = state[0]; calls.append(key)
            if len(calls) == 1:
                entered.set(); assert release.wait(2)
            return value
        cache = Cache(loader)
        job = launch(lambda: cache.get('x'))
        self.assertTrue(entered.wait(1))
        forget = launch(lambda: cache.invalidate('x'))
        try:
            finish(forget); state[0] = 2
        finally: release.set()
        self.assertEqual(finish(job), 2)
        self.assertEqual(cache.get('x'), 2)
    def test_singleflight(self):
        entered, release = threading.Event(), threading.Event(); calls = []
        def loader(key):
            calls.append(key); entered.set(); assert release.wait(2); return 7
        cache = Cache(loader)
        jobs = [launch(lambda: cache.get('x')) for _ in range(4)]
        self.assertTrue(entered.wait(1)); release.set()
        self.assertEqual([finish(j) for j in jobs], [7]*4)
        self.assertEqual(len(calls), 1)
''')
SYNC_HIDDEN = code('''
from cache import Cache
class Regression(unittest.TestCase):
    def test_reentry_other_key_and_same_key(self):
        box = {}
        def loader(k):
            if k == 'a': return box['c'].get('b') + 1
            if k == 'loop': return box['c'].get('loop')
            return 4
        box['c'] = Cache(loader)
        self.assertEqual(finish(launch(lambda: box['c'].get('a'))), 5)
        with self.assertRaises(RuntimeError): finish(launch(lambda: box['c'].get('loop')))
    def test_exception_does_not_poison(self):
        calls = []
        def loader(k):
            calls.append(k)
            if len(calls) == 1: raise ValueError('transient')
            return None
        cache = Cache(loader)
        with self.assertRaises(ValueError): cache.get('x')
        self.assertIsNone(cache.get('x')); self.assertIsNone(cache.get('x'))
        self.assertEqual(len(calls), 2)
''')

ASYNC_FIXED = code('''
import asyncio

class AsyncCache:
    def __init__(self, loader):
        self.loader = loader
        self.values, self.epochs, self.jobs = {}, {}, {}

    def invalidate(self, key):
        self.epochs[key] = self.epochs.get(key, 0) + 1
        self.values.pop(key, None)

    async def _load(self, key, epoch):
        try:
            result = await self.loader(key)
            if self.epochs.get(key, 0) == epoch: self.values[key] = result
            return result
        finally:
            record = self.jobs.get(key)
            if record and record[1] is asyncio.current_task(): self.jobs.pop(key)

    async def get(self, key):
        while True:
            if key in self.values: return self.values[key]
            epoch = self.epochs.get(key, 0)
            record = self.jobs.get(key)
            if not record or record[0] != epoch:
                record = (epoch, asyncio.create_task(self._load(key, epoch)))
                self.jobs[key] = record
            if record[1] is asyncio.current_task(): raise RuntimeError('recursive same-key load')
            result = await asyncio.shield(record[1])
            if self.epochs.get(key, 0) == epoch: return result
''')
ASYNC_PUBLIC = code('''
from async_cache import AsyncCache
class Contract(unittest.TestCase):
    def test_cancel_isolation(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event(); calls = []
            async def loader(k):
                calls.append(k); entered.set(); await release.wait(); return 8
            cache = AsyncCache(loader)
            one = asyncio.create_task(cache.get('x'))
            two = asyncio.create_task(cache.get('x'))
            await entered.wait(); await asyncio.sleep(0); one.cancel()
            with self.assertRaises(asyncio.CancelledError): await one
            release.set()
            self.assertEqual(await two, 8); self.assertEqual(await cache.get('x'), 8)
            self.assertEqual(len(calls), 1)
        asyncio.run(asyncio.wait_for(scenario(), 3))
    def test_inflight_invalidation(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event(); calls = []; state = [1]
            async def loader(k):
                value = state[0]; calls.append(k)
                if len(calls) == 1: entered.set(); await release.wait()
                return value
            cache = AsyncCache(loader); pending = asyncio.create_task(cache.get('x'))
            await entered.wait(); state[0] = 9; cache.invalidate('x'); release.set()
            self.assertEqual(await pending, 9); self.assertEqual(await cache.get('x'), 9)
        asyncio.run(asyncio.wait_for(scenario(), 3))
''')
ASYNC_HIDDEN = code('''
from async_cache import AsyncCache
class Regression(unittest.TestCase):
    def test_error_retry_and_none(self):
        async def scenario():
            calls = []
            async def loader(k):
                calls.append(k)
                if len(calls) == 1: raise ValueError('retry')
                return None
            cache = AsyncCache(loader)
            with self.assertRaises(ValueError): await cache.get('x')
            self.assertIsNone(await cache.get('x')); self.assertIsNone(await cache.get('x'))
            self.assertEqual(len(calls), 2)
        asyncio.run(asyncio.wait_for(scenario(), 3))
    def test_recursive_load(self):
        async def scenario():
            async def loader(k): return await cache.get(k)
            cache = AsyncCache(loader)
            with self.assertRaises(RuntimeError): await cache.get('x')
        asyncio.run(asyncio.wait_for(scenario(), 3))
''')

TTL_FIXED = code('''
from collections import OrderedDict
class TTLCache:
    def __init__(self, clock, ttl=10, capacity=2):
        if ttl <= 0 or capacity <= 0: raise ValueError('invalid limits')
        self.clock, self.ttl, self.capacity = clock, ttl, capacity
        self.items = OrderedDict()
    def get(self, key, loader):
        now = self.clock()
        if key in self.items:
            value, deadline = self.items[key]
            if now < deadline:
                self.items.move_to_end(key); return value
            del self.items[key]
        value = loader(key)
        self.items[key] = (value, self.clock() + self.ttl)
        self.items.move_to_end(key)
        while len(self.items) > self.capacity: self.items.popitem(last=False)
        return value
''')
TTL_PUBLIC = code('''
from ttl_cache import TTLCache
class Contract(unittest.TestCase):
    def test_ttl_is_policy_not_store_version(self):
        clock, data = [0], [1]; calls = []
        def load(k): calls.append(k); return data[0]
        cache = TTLCache(lambda: clock[0], ttl=10)
        self.assertEqual(cache.get('x', load), 1)
        data[0] = 2; clock[0] = 9
        self.assertEqual(cache.get('x', load), 1)
        clock[0] = 10
        self.assertEqual(cache.get('x', load), 2); self.assertEqual(len(calls), 2)
    def test_none_is_a_cached_value(self):
        calls = []
        cache = TTLCache(lambda: 0)
        def load(k): calls.append(k); return None
        self.assertIsNone(cache.get('x', load)); self.assertIsNone(cache.get('x', load))
        self.assertEqual(len(calls), 1)
''')
TTL_HIDDEN = code('''
from ttl_cache import TTLCache
class Regression(unittest.TestCase):
    def test_expiry_starts_at_completion_and_lru(self):
        t = [0]; calls = []
        def load(k): calls.append(k); t[0] += 5; return k
        c = TTLCache(lambda:t[0], ttl=10, capacity=2)
        c.get('a', load); t[0] = 14; self.assertEqual(c.get('a',load), 'a')
        self.assertEqual(calls, ['a'])
        c.get('b',load); c.get('a',load); c.get('c',load)
        c.get('b',load); self.assertEqual(calls.count('b'), 2)
    def test_error_is_not_cached(self):
        c = TTLCache(lambda:0)
        def fail(k): raise ValueError('retry')
        with self.assertRaises(ValueError): c.get('x', fail)
        self.assertEqual(c.get('x',lambda k:3),3)
''')


def specs():
    return [
        dict(id="learn-sync-flight", split="learn", group="handwritten-sync-per-key-jobs",
             module="cache.py", issue="""Repair Cache.get/invalidate for concurrent use. Keep existing API.
             Same-key concurrent misses must share one loader call; different keys must load concurrently.
             Never run the loader while holding the cache lock. invalidate(key) must return without waiting
             for an in-flight loader and fence that work: a value from before invalidation must neither be
             returned by the pending get nor published afterward; the pending get retries the new generation.
             Loader exceptions must wake waiters and not poison future calls. None is cacheable.
             A loader may recursively request a different key; recursive same-key loads must raise RuntimeError
             instead of deadlocking. No change to callers or tests is allowed.""",
             buggy=code('''
             import threading
             class Cache:
                 def __init__(self, loader):
                     self.loader=loader; self.values={}; self.lock=threading.Lock()
                 def get(self, key):
                     with self.lock:
                         if key not in self.values: self.values[key]=self.loader(key)
                         return self.values[key]
                 def invalidate(self, key):
                     with self.lock: self.values.pop(key,None)
             '''), fixed=SYNC_FIXED, public=SYNC_PUBLIC, hidden=SYNC_HIDDEN),
        dict(id="learn-async-flight", split="learn", group="handwritten-async-task-per-key",
             module="async_cache.py", issue="""Repair AsyncCache.get/invalidate without changing its public API.
             Same-key asynchronous callers must share one load. Cancelling one caller must not cancel
             the shared loader or other callers. Invalidation during a load fences old results:
             pending get calls must retry and return the newer generation, not publish stale results.
             Failure must clean pending work so a later call can retry. Cache None normally.
             Loaders may request other keys; a recursive same-key load raises RuntimeError.
             Use asyncio cancellation semantics correctly; no artificial sleeps for synchronization.""",
             buggy=code('''
             import asyncio
             class AsyncCache:
                 def __init__(self, loader): self.loader=loader; self.values={}; self.jobs={}
                 async def get(self,key):
                     if key in self.values: return self.values[key]
                     if key not in self.jobs: self.jobs[key]=asyncio.create_task(self.loader(key))
                     result=await self.jobs[key]
                     self.values[key]=result; self.jobs.pop(key,None)
                     return result
                 def invalidate(self,key): self.values.pop(key,None)
             '''), fixed=ASYNC_FIXED, public=ASYNC_PUBLIC, hidden=ASYNC_HIDDEN),
        dict(id="learn-ttl-policy", split="learn", group="handwritten-ordered-ttl-lru",
             module="ttl_cache.py", issue="""Repair TTLCache(clock, ttl=10, capacity=2).get(key,loader).
             This cache deliberately permits stale source values until TTL expiry; it has NO revision
             protocol, so do not replace TTL with source-version invalidation. A hit requires now < deadline;
             equality expires. TTL starts after a successful loader completes. None is a valid cached value.
             Loader failures are not cached. Keep at most capacity successful entries, evict least recently
             used, and count successful hits as use. Positive ttl/capacity are required. Keep the API.""",
             buggy=code('''
             class TTLCache:
                 def __init__(self,clock,ttl=10,capacity=2):
                     self.clock=clock; self.ttl=ttl; self.capacity=capacity; self.items={}
                 def get(self,key,loader):
                     now=self.clock(); value,stamp=self.items.get(key,(None,now))
                     if value is not None and now-stamp <= self.ttl: return value
                     value=loader(key); self.items[key]=(value,now); return value
             '''), fixed=TTL_FIXED, public=TTL_PUBLIC, hidden=TTL_HIDDEN),
        *development_specs(), *final_specs(),
    ]


def development_specs():
    memo_fixed = code('''
    import threading
    class Memo:
        def __init__(self,fetcher):
            self.fetcher=fetcher; self.data={}; self.generation=0
            self.busy={}; self.cv=threading.Condition()
        def clear(self):
            with self.cv:
                self.generation+=1; self.data.clear(); self.busy.clear(); self.cv.notify_all()
        def fetch(self,key):
            while True:
                with self.cv:
                    if key in self.data: return self.data[key]
                    if key in self.busy:
                        self.cv.wait(); continue
                    ticket=(self.generation,object()); self.busy[key]=ticket
                try: value=self.fetcher(key)
                except BaseException:
                    with self.cv:
                        if self.busy.get(key) is ticket: self.busy.pop(key)
                        self.cv.notify_all()
                    raise
                with self.cv:
                    if self.busy.get(key) is ticket: self.busy.pop(key)
                    self.cv.notify_all()
                    if ticket[0] != self.generation: continue
                    self.data[key]=value; return value
    ''')
    memo_public = code('''
    from memo import Memo
    class Contract(unittest.TestCase):
        def test_clear_during_fetch(self):
            started,release=threading.Event(),threading.Event(); state=[2]; calls=[]
            def loader(k):
                v=state[0]; calls.append(k)
                if len(calls)==1: started.set(); assert release.wait(2)
                return v
            m=Memo(loader); job=launch(lambda:m.fetch('a'))
            self.assertTrue(started.wait(1)); state[0]=6
            try: finish(launch(m.clear))
            finally: release.set()
            self.assertEqual(finish(job),6); self.assertEqual(m.fetch('a'),6)
    ''')
    memo_hidden = code('''
    from memo import Memo
    class Regression(unittest.TestCase):
        def test_two_keys_can_enter_loader(self):
            a,b,release=threading.Event(),threading.Event(),threading.Event()
            def loader(k):
                (a if k=='a' else b).set(); assert release.wait(2); return k
            m=Memo(loader); ja=launch(lambda:m.fetch('a')); self.assertTrue(a.wait(1))
            jb=launch(lambda:m.fetch('b'))
            try: self.assertTrue(b.wait(1))
            finally: release.set()
            self.assertEqual(finish(ja),'a'); self.assertEqual(finish(jb),'b')
        def test_none_is_memoized(self):
            calls=[]; m=Memo(lambda k:calls.append(k))
            self.assertIsNone(m.fetch('a')); self.assertIsNone(m.fetch('a')); self.assertEqual(calls,['a'])
    ''')
    async_fixed = code('''
    import asyncio
    class AsyncMemo:
        def __init__(self, source): self.source=source; self.values={}; self.pending={}; self.epoch=0
        def clear(self): self.epoch+=1; self.values.clear()
        async def _produce(self,key,epoch):
            try:
                result=await self.source(key)
                if self.epoch==epoch: self.values[key]=result
                return result
            finally:
                current=self.pending.get(key)
                if current and current[1] is asyncio.current_task(): self.pending.pop(key)
        async def resolve(self,key):
            while True:
                if key in self.values: return self.values[key]
                epoch=self.epoch; job=self.pending.get(key)
                if not job or job[0]!=epoch:
                    job=(epoch,asyncio.create_task(self._produce(key,epoch))); self.pending[key]=job
                value=await asyncio.shield(job[1])
                if self.epoch==epoch: return value
    ''')
    async_public = code('''
    from async_memo import AsyncMemo
    class Contract(unittest.TestCase):
        def test_cancel_one_subscriber(self):
            async def scenario():
                started,release=asyncio.Event(),asyncio.Event(); calls=[]
                async def loader(k): calls.append(k); started.set(); await release.wait(); return 5
                c=AsyncMemo(loader); a=asyncio.create_task(c.resolve('x')); b=asyncio.create_task(c.resolve('x'))
                await started.wait(); await asyncio.sleep(0); a.cancel()
                with self.assertRaises(asyncio.CancelledError): await a
                release.set(); self.assertEqual(await b,5); self.assertEqual(calls,['x'])
            asyncio.run(asyncio.wait_for(scenario(),3))
    ''')
    async_hidden = code('''
    from async_memo import AsyncMemo
    class Regression(unittest.TestCase):
        def test_clear_fences_old_generation(self):
            async def scenario():
                entered,release=asyncio.Event(),asyncio.Event(); data=[1]; calls=[]
                async def load(k):
                    value=data[0]; calls.append(k)
                    if len(calls)==1: entered.set(); await release.wait()
                    return value
                c=AsyncMemo(load); pending=asyncio.create_task(c.resolve('a'))
                await entered.wait(); data[0]=12; c.clear(); release.set()
                self.assertEqual(await pending,12); self.assertEqual(await c.resolve('a'),12)
            asyncio.run(asyncio.wait_for(scenario(),3))
    ''')
    store = code('''
    class Store:
        def __init__(self): self.version=0; self.data={'doc':{'tags':['base']}}; self.calls=0
        def set(self,key,value): self.data[key]=value; self.version+=1
        def read(self,key): self.calls+=1; return self.data[key]
    ''')
    snapshot_fixed = code('''
    import copy
    class SnapshotCache:
        def __init__(self,store): self.store=store; self.version=None; self.cached={}
        def get(self,key):
            if self.version != self.store.version:
                self.cached.clear(); self.version=self.store.version
            if key not in self.cached: self.cached[key]=copy.deepcopy(self.store.read(key))
            return copy.deepcopy(self.cached[key])
    ''')
    snapshot_public = code('''
    from snapshot import SnapshotCache
    from backing import Store
    class Contract(unittest.TestCase):
        def test_nested_results_are_detached(self):
            s=Store(); c=SnapshotCache(s); first=c.get('doc'); first['tags'].append('caller')
            self.assertEqual(s.data['doc'],{'tags':['base']})
            self.assertEqual(c.get('doc'),{'tags':['base']}); self.assertEqual(s.calls,1)
    ''')
    snapshot_hidden = code('''
    from snapshot import SnapshotCache
    from backing import Store
    class Regression(unittest.TestCase):
        def test_external_write_and_source_alias(self):
            s=Store(); c=SnapshotCache(s); c.get('doc')
            s.set('doc', {'tags':['new']}); self.assertEqual(c.get('doc'),{'tags':['new']})
            s.data['doc']['tags'].append('out-of-band')
            self.assertEqual(c.get('doc'),{'tags':['new']})
    ''')
    keyed_store = code('''
    class Store:
        def __init__(self): self.values={'a':1,'b':2}; self.versions={'a':0,'b':0}; self.calls=[]
        def revision_for(self,k): return self.versions.get(k,0)
        def write(self,k,v): self.values[k]=v; self.versions[k]=self.revision_for(k)+1
        def load(self,k): self.calls.append(k); return self.values.get(k)
    ''')
    keyed_fixed = code('''
    class KeyCache:
        def __init__(self,store): self.store=store; self.items={}
        def get(self,key):
            version=self.store.revision_for(key)
            entry=self.items.get(key)
            if entry is None or entry[0]!=version:
                entry=(version,self.store.load(key)); self.items[key]=entry
            return entry[1]
    ''')
    keyed_public = code('''
    from keyed import KeyCache
    from source import Store
    class Contract(unittest.TestCase):
        def test_selective_invalidation(self):
            s=Store(); c=KeyCache(s); c.get('a'); c.get('b'); s.write('a',5)
            self.assertEqual(c.get('b'),2); self.assertEqual(c.get('a'),5)
            self.assertEqual(s.calls,['a','b','a'])
    ''')
    keyed_hidden = code('''
    from keyed import KeyCache
    from source import Store
    class Regression(unittest.TestCase):
        def test_negative_lookup_is_cacheable(self):
            s=Store(); c=KeyCache(s)
            self.assertIsNone(c.get('missing')); self.assertIsNone(c.get('missing'))
            self.assertEqual(s.calls,['missing']); s.write('missing',0)
            self.assertEqual(c.get('missing'),0)
    ''')
    return [
        dict(id="dev-sync-condition", split="dev", group="handwritten-sync-wholecache-condition",
             module="memo.py", issue="""Fix Memo.fetch(key)/clear(). Share same-key misses, allow distinct-key
             fetchers to run concurrently, and never hold the condition lock while calling the fetcher.
             clear() must finish even when a fetch is blocked; older work must not publish or return stale
             values after clear: retry that fetch against the current generation. Exceptions wake waiting
             callers and permit retries. None is a successful cached value. Preserve public interfaces.""",
             buggy=code('''
             import threading
             class Memo:
                 def __init__(self,fetcher): self.fetcher=fetcher; self.data={}; self.cv=threading.Condition()
                 def fetch(self,key):
                     with self.cv:
                         if key not in self.data: self.data[key]=self.fetcher(key)
                         return self.data[key]
                 def clear(self):
                     with self.cv: self.data.clear()
             '''), fixed=memo_fixed, public=memo_public, hidden=memo_hidden),
        dict(id="dev-async-global", split="dev", group="handwritten-async-global-clear",
             module="async_memo.py", issue="""Repair AsyncMemo(source).resolve(key) and synchronous clear().
             Coalesce same-key loads, isolate one waiter's cancellation from shared work, cache successful
             None, clean failed work for retry, and fence every outstanding load on clear(). A resolve
             already waiting before clear must retry and return the new generation. Keep async interfaces.""",
             buggy=code('''
             import asyncio
             class AsyncMemo:
                 def __init__(self,source): self.source=source; self.values={}; self.pending={}
                 async def resolve(self,key):
                     if key in self.values: return self.values[key]
                     task=self.pending.setdefault(key,asyncio.create_task(self.source(key)))
                     value=await task; self.values[key]=value; return value
                 def clear(self): self.values.clear()
             '''), fixed=async_fixed, public=async_public, hidden=async_hidden),
        dict(id="dev-snapshot-alias", split="dev", group="handwritten-detached-snapshots",
             module="snapshot.py", extra={"backing.py":store}, issue="""Fix SnapshotCache.get so each caller
             receives an independent deep snapshot of the cached document. Mutating returned nested data
             must not mutate the backing store or cache. Refresh when store.version changes, keep serving
             cache hits without extra store.read calls, and keep the cached snapshot isolated even if the
             source is later mutated out-of-band without a version change. backing.py is read-only.""",
             buggy=code('''
             class SnapshotCache:
                 def __init__(self,store): self.store=store; self.version=None; self.cached={}
                 def get(self,key):
                     if self.version!=self.store.version:
                         self.cached.clear(); self.version=self.store.version
                     if key not in self.cached: self.cached[key]=self.store.read(key)
                     return self.cached[key]
             '''), fixed=snapshot_fixed, public=snapshot_public, hidden=snapshot_hidden),
        dict(id="dev-keyed-revisions", split="dev", group="handwritten-selective-key-revisions",
             module="keyed.py", extra={"source.py":keyed_store}, issue="""Fix KeyCache.get using the actual
             store.revision_for(key) protocol. The store has NO global revision attribute. Refresh only
             changed keys; unrelated writes must preserve hot cached keys without extra loads. Negative
             lookups returning None and values 0/False are cacheable. source.py is not editable.""",
             buggy=code('''
             class KeyCache:
                 def __init__(self,store): self.store=store; self.items={}
                 def get(self,key):
                     if key not in self.items: self.items[key]=self.store.load(key)
                     return self.items[key]
             '''), fixed=keyed_fixed, public=keyed_public, hidden=keyed_hidden),
    ]


def final_specs():
    return [
        dict(id="test-identity-reset", split="test", group="handwritten-source-incarnation-aba",
             module="identity_cache.py", extra={"backend.py":code('''
             class Backend:
                 def __init__(self): self.identity=1; self.revision=0; self.value='old'; self.calls=0
                 def replace(self,value): self.identity+=1; self.revision=0; self.value=value
                 def write(self,value): self.revision+=1; self.value=value
                 def load(self): self.calls+=1; return self.value
             ''')}, issue="""Fix IdentityCache.get when its backend is replaced. Backend.revision resets to
             zero on replace(), so revision alone cannot identify fresh content. The backend.identity
             identifies its incarnation. Preserve hit caching, refresh on either identity or revision
             change, accept None/False, and do not alter backend.py. Calls in this task are single-threaded.""",
             buggy=code('''
             class IdentityCache:
                 def __init__(self,backend): self.backend=backend; self.token=None; self.value=None
                 def get(self):
                     if self.token!=self.backend.revision:
                         self.value=self.backend.load(); self.token=self.backend.revision
                     return self.value
             '''), fixed=code('''
             class IdentityCache:
                 def __init__(self,backend): self.backend=backend; self.token=None; self.value=None
                 def get(self):
                     token=(self.backend.identity,self.backend.revision)
                     if token!=self.token: self.value=self.backend.load(); self.token=token
                     return self.value
             '''), public=code('''
             from identity_cache import IdentityCache
             from backend import Backend
             class Contract(unittest.TestCase):
                 def test_replacement_same_revision(self):
                     b=Backend(); c=IdentityCache(b); self.assertEqual(c.get(),'old')
                     b.replace('new'); self.assertEqual(c.get(),'new'); self.assertEqual(b.calls,2)
             '''), hidden=code('''
             from identity_cache import IdentityCache
             from backend import Backend
             class Regression(unittest.TestCase):
                 def test_hits_none_and_writes(self):
                     b=Backend(); c=IdentityCache(b); c.get(); c.get(); self.assertEqual(b.calls,1)
                     b.replace(None); self.assertIsNone(c.get()); self.assertIsNone(c.get())
                     b.write(False); self.assertIs(c.get(),False); self.assertEqual(b.calls,3)
             ''')),
        dict(id="test-ttl-negative", split="test", group="handwritten-deadline-negative-cache",
             module="deadline.py", issue="""Repair DeadlineMap(now,seconds).read(key,fallback). This is a
             TTL-policy cache with no source version protocol. None/False/0 returned by fallback are
             valid cached results. An entry expires exactly at its deadline; successful refresh starts
             its lifetime after fallback returns. Failures must not be cached, no source polling on hits.
             It is correct to return an older source value until TTL expires. Preserve the public API.""",
             buggy=code('''
             class DeadlineMap:
                 def __init__(self,now,seconds): self.now=now; self.seconds=seconds; self.data={}
                 def read(self,key,fallback):
                     value,expires=self.data.get(key,(None,-1))
                     if value and self.now()<=expires: return value
                     expires=self.now()+self.seconds; value=fallback(key)
                     self.data[key]=(value,expires); return value
             '''), fixed=code('''
             class DeadlineMap:
                 def __init__(self,now,seconds): self.now=now; self.seconds=seconds; self.data={}
                 def read(self,key,fallback):
                     if key in self.data:
                         value,expires=self.data[key]
                         if self.now()<expires: return value
                     value=fallback(key); self.data[key]=(value,self.now()+self.seconds); return value
             '''), public=code('''
             from deadline import DeadlineMap
             class Contract(unittest.TestCase):
                 def test_negative_and_boundary(self):
                     now=[0]; values=[None]; calls=[]
                     def fallback(k): calls.append(k); return values[0]
                     d=DeadlineMap(lambda:now[0],5)
                     self.assertIsNone(d.read('x',fallback)); values[0]=0; now[0]=4
                     self.assertIsNone(d.read('x',fallback)); self.assertEqual(calls,['x'])
                     now[0]=5; self.assertEqual(d.read('x',fallback),0)
             '''), hidden=code('''
             from deadline import DeadlineMap
             class Regression(unittest.TestCase):
                 def test_completion_time(self):
                     now=[0]; calls=[]
                     def fallback(k): calls.append(k); now[0]+=4; return False
                     d=DeadlineMap(lambda:now[0],5); self.assertIs(d.read('x',fallback),False)
                     now[0]=8; self.assertIs(d.read('x',fallback),False); self.assertEqual(calls,['x'])
             ''')),
    ]
