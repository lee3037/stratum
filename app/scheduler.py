"""解析进程池的优先级调度。

ProcessPoolExecutor 按提交顺序先进先出：一个批量任务一次提交几百个页面分组后，随后到达的单个解析要排在它们全部后面。
这里在进程池前加一层调度：

- 只把「空闲进程数」个任务交给进程池，其余留在本地优先级队列中；
- 交互解析（单个文件）优先于批量解析；已在执行的页面分组不中断，所以最多等一个分组（≤8 页）；
- 同一优先级内按各文件的分组序号轮转（第 1 组、第 1 组、第 2 组……），几个人同时解析时各自都有进展，不会一个文件独占；
- 文件解析结束（成功或失败）后，撤下它尚未派发的分组；
- 工作进程意外退出（内存不足、PDF 触发底层崩溃）会让整个进程池失效：自动重建进程池，受影响的分组重试一次。
"""
from __future__ import annotations

import heapq
import itertools
import threading
from concurrent.futures import Executor, Future
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from typing import Callable

INTERACTIVE = 0
BATCH = 1
PRIORITY_NAMES = {INTERACTIVE: "interactive", BATCH: "batch"}
CRASH_MESSAGE = "解析进程意外退出（可能内存不足，或 PDF 触发了底层解析错误），请稍后重试；反复出现请联系管理员"


class _Task:
    __slots__ = ("fn", "args", "future", "owner", "priority", "retries")

    def __init__(self, fn, args, future, owner, priority):
        self.fn, self.args, self.future, self.owner, self.priority = fn, args, future, owner, priority
        self.retries = 0


class PriorityPool:
    def __init__(self, factory: Callable[[], Executor], slots: int, max_retries: int = 1):
        """factory() 创建底层执行器（生产中为进程池）；slots 为同时交给它的任务数，通常等于进程数。"""
        self._factory = factory
        self._slots = max(1, slots)
        self._max_retries = max_retries
        self._cond = threading.Condition()
        self._heap: list[tuple] = []
        self._seq = itertools.count()
        self._owner_seq = itertools.count()
        self._submitted: dict[str, int] = {}  # owner → 已提交的分组数，作为同优先级内的轮转序号
        self._owners: dict[str, int] = {}  # owner → 尚未结束的分组数
        self._inflight = 0
        self._executor: Executor | None = None
        self._generation = 0  # 进程池重建次数；旧池的失败回调据此识别
        self._closed = False
        self._thread: threading.Thread | None = None
        self.restarts = 0

    # ------------------------------------------------------------ 对外

    @contextmanager
    def session(self, priority: int, label: str = ""):
        """一次文件解析使用的执行器视图；退出时撤下尚未派发的分组。"""
        owner = f"{label or 'job'}-{next(self._owner_seq)}"
        view = _View(self, owner, priority)
        try:
            yield view
        finally:
            self.cancel_owner(owner)

    def submit(self, owner: str, priority: int, fn, *args) -> Future:
        future: Future = Future()
        with self._cond:
            if self._closed:
                raise RuntimeError("解析服务正在关闭")
            task = _Task(fn, args, future, owner, priority)
            self._push(task)
            self._owners[owner] = self._owners.get(owner, 0) + 1
            self._ensure_thread()
            self._cond.notify_all()
        return future

    def cancel_owner(self, owner: str) -> int:
        """撤下 owner 尚未派发的分组，返回撤下的数量。已在进程中执行的分组不受影响。"""
        cancelled = 0
        with self._cond:
            kept = []
            for entry in self._heap:
                task = entry[-1]
                if task.owner == owner:
                    if not task.future.cancel():  # 重试中的分组已是运行状态，无法 cancel
                        task.future.set_exception(RuntimeError("解析已结束，未执行的分组已撤下"))
                    cancelled += 1
                    self._release_owner(owner)
                else:
                    kept.append(entry)
            if cancelled:
                self._heap = kept
                heapq.heapify(self._heap)
        return cancelled

    def stats(self) -> dict:
        with self._cond:
            pending = {name: 0 for name in PRIORITY_NAMES.values()}
            owners = {name: set() for name in PRIORITY_NAMES.values()}
            for entry in self._heap:
                task = entry[-1]
                name = PRIORITY_NAMES.get(task.priority, str(task.priority))
                pending[name] = pending.get(name, 0) + 1
                owners.setdefault(name, set()).add(task.owner)
            return {"slots": self._slots, "running": self._inflight, "pending": pending,
                    "waiting_files": {k: len(v) for k, v in owners.items()}, "restarts": self.restarts}

    def start(self) -> None:
        """服务启动时调用；与 shutdown 配对，允许同一进程内重复启动（测试中多次进入 lifespan）。"""
        with self._cond:
            self._closed = False

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            for entry in self._heap:
                entry[-1].future.cancel()
            self._heap.clear()
            executor, self._executor = self._executor, None
            self._cond.notify_all()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ 内部（调用时持有锁）

    def _push(self, task: _Task) -> None:
        order = self._submitted.get(task.owner, 0)
        self._submitted[task.owner] = order + 1
        heapq.heappush(self._heap, (task.priority, order, next(self._seq), task))

    def _release_owner(self, owner: str) -> None:
        left = self._owners.get(owner, 0) - 1
        if left > 0:
            self._owners[owner] = left
        else:
            self._owners.pop(owner, None)
            self._submitted.pop(owner, None)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._dispatch_loop, name="pool-dispatch", daemon=True)
            self._thread.start()

    def _executor_locked(self) -> tuple[Executor, int]:
        if self._executor is None:
            self._executor = self._factory()
        return self._executor, self._generation

    def _broken_locked(self, generation: int) -> None:
        """底层进程池失效：丢弃它，下次派发时重建。同一次失效只处理一次。"""
        if generation != self._generation:
            return
        self._generation += 1
        self.restarts += 1
        old, self._executor = self._executor, None
        if old is not None:
            threading.Thread(target=old.shutdown, kwargs={"wait": False, "cancel_futures": True},
                             daemon=True).start()

    # ------------------------------------------------------------ 派发

    def _dispatch_loop(self) -> None:
        # 注意：底层进程池在持有内部锁时执行完成回调（_done 需要 self._cond），
        # 所以调用 executor.submit 时绝不能持有 self._cond，否则会互相等待。
        while True:
            with self._cond:
                while not self._closed and (not self._heap or self._inflight >= self._slots):
                    self._cond.wait(timeout=5)
                if self._closed:
                    return
                *_, task = heapq.heappop(self._heap)
                if not task.future.running() and not task.future.set_running_or_notify_cancel():
                    self._release_owner(task.owner)
                    continue
                self._inflight += 1
                try:
                    executor, generation = self._executor_locked()
                except Exception as exc:  # noqa: BLE001  进程池无法创建
                    self._inflight -= 1
                    self._release_owner(task.owner)
                    task.future.set_exception(exc)
                    continue
            try:
                inner = executor.submit(task.fn, *task.args)
            except Exception as exc:  # noqa: BLE001
                with self._cond:
                    self._inflight -= 1
                    if isinstance(exc, (BrokenProcessPool, RuntimeError)) and not self._closed:
                        # 进程池已失效或已被关闭：重建后重试
                        self._broken_locked(generation)
                        self._retry_or_fail(task, BrokenProcessPool(CRASH_MESSAGE))
                    else:
                        self._release_owner(task.owner)
                        task.future.set_exception(exc)
                continue
            inner.add_done_callback(lambda f, t=task, g=generation: self._done(t, g, f))

    def _done(self, task: _Task, generation: int, inner: Future) -> None:
        with self._cond:
            self._inflight -= 1
            self._cond.notify_all()
            exc = None if inner.cancelled() else inner.exception()
            if isinstance(exc, BrokenProcessPool) or (inner.cancelled() and generation != self._generation):
                self._broken_locked(generation)
                self._retry_or_fail(task, BrokenProcessPool(CRASH_MESSAGE))
                return
            self._release_owner(task.owner)
        if inner.cancelled():
            task.future.set_exception(RuntimeError("解析服务正在关闭"))
        elif exc is not None:
            task.future.set_exception(exc)
        else:
            task.future.set_result(inner.result())

    def _retry_or_fail(self, task: _Task, exc: BaseException) -> None:
        """进程池失效时，受影响的分组重新排队一次；再次失效则报告给调用方。调用时持有锁。"""
        if task.retries < self._max_retries and not self._closed:
            task.retries += 1
            heapq.heappush(self._heap, (task.priority, -1, next(self._seq), task))  # 优先重试
            self._cond.notify_all()
            return
        self._release_owner(task.owner)
        task.future.set_exception(exc)


class _View(Executor):
    """交给 pipeline.convert 的执行器：submit 带上任务归属与优先级。"""

    def __init__(self, pool: PriorityPool, owner: str, priority: int):
        self._pool, self.owner, self.priority = pool, owner, priority

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        if kwargs:
            raise TypeError("不支持关键字参数")
        return self._pool.submit(self.owner, self.priority, fn, *args)

    def shutdown(self, wait=True, *, cancel_futures=False):  # 生命周期由 PriorityPool 管理
        pass
