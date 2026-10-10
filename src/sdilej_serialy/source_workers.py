"""Bounded discovery workers; only the caller publishes queue/state changes."""
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def inspected_results(items, provider, inspect, *, workers, limit, deadline, tick):
    if workers == 1:
        for index, item in enumerate(items):
            if index >= limit or time.monotonic() >= deadline:
                break
            yield item, inspect(provider, item)
        return
    local = threading.local()
    providers = []
    provider_lock = threading.Lock()

    def evaluate(item):
        if not hasattr(local, 'provider'):
            local.provider = provider.fork_worker()
            with provider_lock:
                providers.append(local.provider)
        return inspect(local.provider, item)

    iterator = iter(items)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='source')
    futures, submitted = {}, 0
    last_tick = time.monotonic()
    try:
        while True:
            while len(futures) < workers and submitted < limit and time.monotonic() < deadline:
                item = next(iterator, None)
                if item is None:
                    break
                future = pool.submit(evaluate, item)
                futures[future] = (submitted, item)
                submitted += 1
            if not futures:
                break
            done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            if time.monotonic() - last_tick >= 30:
                tick()
                last_tick = time.monotonic()
            # Preserve dispatch priority among simultaneously completed work.
            # Do not hold a finished result behind an unrelated slow source.
            for future in sorted(done, key=lambda f: futures[f][0]):
                _, item = futures.pop(future)
                yield item, future.result()
    finally:
        # An unsuccessful publication prevents any additional dispatch. Work
        # already running is read-only; cancel unstarted futures and close all
        # worker sessions before the sole publisher exits.
        pool.shutdown(wait=True, cancel_futures=True)
        for worker in providers:
            worker.session.close()
