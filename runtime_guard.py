"""Coordinate requests, background jobs and replacement of the local data set."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
import threading

from apscheduler.executors.pool import BasePoolExecutor


class DataBusyError(RuntimeError):
    pass


class DataCoordinator:
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.active = {}
        self.pending_tasks = 0
        self.maintenance_owner = None
        self.writer = None

    @contextmanager
    def operation(self, mutating=False):
        owner = threading.get_ident()
        with self.condition:
            if self.maintenance_owner not in (None, owner):
                raise DataBusyError("Probíhá obnova nebo záloha dat. Zkuste akci znovu za chvíli.")
            if mutating and (self.pending_tasks or self.writer not in (None, owner)):
                raise DataBusyError("Probíhá kontrola nebo jiná změna dat. Počkejte na její dokončení.")
            self.active[owner] = self.active.get(owner, 0) + 1
            if mutating:
                self.writer = owner
        try:
            yield
        finally:
            with self.condition:
                self.active[owner] -= 1
                if not self.active[owner]:
                    del self.active[owner]
                if mutating:
                    self.writer = None
                self.condition.notify_all()

    @contextmanager
    def maintenance(self):
        owner = threading.get_ident()
        with self.condition:
            if self.maintenance_owner == owner:
                nested = True
            else:
                nested = False
                if self.maintenance_owner is not None or self.pending_tasks or any(
                    ident != owner and count for ident, count in self.active.items()
                ):
                    raise DataBusyError("Data právě používá jiná operace. Počkejte na dokončení kontrol a zkuste akci znovu.")
                self.maintenance_owner = owner
        try:
            yield
        finally:
            if not nested:
                with self.condition:
                    self.maintenance_owner = None
                    self.condition.notify_all()

    def reserve_task(self, wait=False):
        with self.condition:
            while self.maintenance_owner is not None:
                if not wait:
                    raise DataBusyError("Probíhá obnova nebo záloha dat.")
                self.condition.wait()
            self.pending_tasks += 1
        released = False

        def release():
            nonlocal released
            with self.condition:
                if not released:
                    self.pending_tasks -= 1
                    released = True
                    self.condition.notify_all()
        return release


coordinator = DataCoordinator()


class CoordinatedPool(ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        release = coordinator.reserve_task(wait=True)
        def run_after_request():
            with coordinator.condition:
                while coordinator.writer is not None:
                    coordinator.condition.wait()
            return fn(*args, **kwargs)
        try:
            future = super().submit(run_after_request)
        except BaseException:
            release()
            raise
        future.add_done_callback(lambda _: release())
        return future


class CoordinatedExecutor(BasePoolExecutor):
    def __init__(self):
        # One worker also prevents scheduled ISIR checks and AI from overlapping.
        super().__init__(CoordinatedPool(max_workers=1))


def add_background_job(scheduler, function, *, args=None, id=None, **kwargs):
    """Reserve data as soon as an immediate job is queued, not just when it starts."""
    release = coordinator.reserve_task()

    @wraps(function)
    def run():
        try:
            return function(*(args or []))
        finally:
            release()

    try:
        return scheduler.add_job(run, id=id, misfire_grace_time=None, **kwargs)
    except BaseException:
        release()
        raise
