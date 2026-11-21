"""Bounded independent work: never queue a whole paid experiment in advance."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def bounded_map(function, items, workers, on_result=None):
    iterator = iter(enumerate(items))
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        for _ in range(workers):
            item = next(iterator, None)
            if item is not None:
                pending[pool.submit(function, item[1])] = item[0]
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            completed = []
            for future in done:
                index = pending.pop(future)
                result = future.result()
                results[index] = result
                if on_result:
                    on_result(result, index)
                completed.append(index)
            for _ in completed:
                item = next(iterator, None)
                if item is not None:
                    pending[pool.submit(function, item[1])] = item[0]
    return [results[i] for i in range(len(results))]
