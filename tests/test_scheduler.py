"""解析进程调度：单个解析优先、同优先级轮转、结束后撤下剩余分组、进程崩溃后自动恢复。"""
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scheduler import BATCH, INTERACTIVE, PriorityPool  # noqa: E402


class Recorder:
    """单线程底层执行器：记录任务实际执行顺序；gate 打开前第一个任务阻塞，便于先把队列排满。"""

    def __init__(self):
        self.order: list[str] = []
        self.gate = threading.Event()
        self.created = 0

    def factory(self):
        self.created += 1
        return ThreadPoolExecutor(max_workers=1)

    def task(self, name):
        if name == "block":
            self.gate.wait(5)
        self.order.append(name)
        return name


def wait_until(predicate, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError


def test_interactive_runs_before_queued_batch_work():
    rec = Recorder()
    pool = PriorityPool(rec.factory, slots=1)
    first = pool.submit("b0", BATCH, rec.task, "block")
    wait_until(lambda: pool.stats()["running"] == 1)
    batch = [pool.submit("b1", BATCH, rec.task, f"batch-{i}") for i in range(5)]
    single = [pool.submit("job", INTERACTIVE, rec.task, f"single-{i}") for i in range(2)]
    stats = pool.stats()
    assert stats["pending"] == {"interactive": 2, "batch": 5}
    assert stats["waiting_files"] == {"interactive": 1, "batch": 1}
    rec.gate.set()
    for f in [first, *batch, *single]:
        f.result(5)
    # 正在执行的批量分组不被打断；之后先执行单个解析的全部分组
    assert rec.order == ["block", "single-0", "single-1", *[f"batch-{i}" for i in range(5)]]
    pool.shutdown()


def test_same_priority_files_take_turns():
    rec = Recorder()
    pool = PriorityPool(rec.factory, slots=1)
    blocker = pool.submit("x", INTERACTIVE, rec.task, "block")
    wait_until(lambda: pool.stats()["running"] == 1)
    a = [pool.submit("a", INTERACTIVE, rec.task, f"a{i}") for i in range(3)]
    b = [pool.submit("b", INTERACTIVE, rec.task, f"b{i}") for i in range(3)]
    rec.gate.set()
    for f in [blocker, *a, *b]:
        f.result(5)
    assert rec.order[1:] == ["a0", "b0", "a1", "b1", "a2", "b2"]
    pool.shutdown()


def test_session_end_drops_unstarted_groups_and_works_with_as_completed():
    rec = Recorder()
    pool = PriorityPool(rec.factory, slots=1)
    blocker = pool.submit("x", BATCH, rec.task, "block")
    wait_until(lambda: pool.stats()["running"] == 1)
    with pool.session(INTERACTIVE, "job") as ex:
        futures = [ex.submit(rec.task, f"s{i}") for i in range(4)]
    assert all(f.cancelled() for f in futures)
    assert pool.stats()["pending"]["interactive"] == 0
    rec.gate.set()
    blocker.result(5)
    with pool.session(INTERACTIVE, "job2") as ex:
        futures = [ex.submit(rec.task, f"t{i}") for i in range(3)]
        assert sorted(f.result() for f in as_completed(futures, timeout=5)) == ["t0", "t1", "t2"]
    assert "s0" not in rec.order
    pool.shutdown()


def test_errors_reach_the_caller():
    pool = PriorityPool(lambda: ThreadPoolExecutor(max_workers=2), slots=2)

    def boom():
        raise ValueError("坏页面")
    with pytest.raises(ValueError, match="坏页面"):
        pool.submit("a", INTERACTIVE, boom).result(5)
    assert pool.stats()["running"] == 0
    pool.shutdown()


class FlakyExecutor(ThreadPoolExecutor):
    """模拟进程池失效：标记为 crash 的任务让整个执行器报 BrokenProcessPool。"""

    def __init__(self, owner):
        super().__init__(max_workers=2)
        self.owner = owner

    def submit(self, fn, *args):
        if args and args[0] == "crash" and self.owner.crashes > 0:
            self.owner.crashes -= 1
            from concurrent.futures import Future
            f = Future()
            f.set_exception(BrokenProcessPool("worker died"))
            return f
        return super().submit(fn, *args)


class Flaky:
    def __init__(self, crashes):
        self.crashes = crashes
        self.created = 0

    def factory(self):
        self.created += 1
        return FlakyExecutor(self)


def test_broken_pool_is_rebuilt_and_task_retried_once():
    flaky = Flaky(crashes=1)
    pool = PriorityPool(flaky.factory, slots=2)
    assert pool.submit("a", INTERACTIVE, lambda x: x, "crash").result(5) == "crash"
    assert flaky.created == 2 and pool.stats()["restarts"] == 1
    # 仍能正常处理后续任务
    assert pool.submit("b", BATCH, lambda x: x, "ok").result(5) == "ok"
    pool.shutdown()


def test_repeated_crash_fails_only_that_task():
    flaky = Flaky(crashes=5)
    pool = PriorityPool(flaky.factory, slots=2)
    with pytest.raises(BrokenProcessPool, match="解析进程意外退出"):
        pool.submit("a", INTERACTIVE, lambda x: x, "crash").result(5)
    assert pool.submit("b", INTERACTIVE, lambda x: x, "ok").result(5) == "ok"
    pool.shutdown()


def _exit_worker(code):
    os._exit(code)


def _square(x):
    return x * x


def test_real_process_crash_recovers():
    """真实进程池：一个工作进程直接退出后，进程池重建，其他任务照常完成。"""
    import multiprocessing as mp
    pool = PriorityPool(lambda: ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context("spawn")), slots=2)
    crashed = pool.submit("bad", INTERACTIVE, _exit_worker, 1)
    with pytest.raises(BrokenProcessPool):
        crashed.result(60)
    assert pool.submit("good", INTERACTIVE, _square, 7).result(60) == 49
    assert pool.stats()["restarts"] >= 1
    pool.shutdown()


def test_shutdown_then_start_again():
    pool = PriorityPool(lambda: ThreadPoolExecutor(max_workers=1), slots=1)
    pool.shutdown()
    with pytest.raises(RuntimeError):
        pool.submit("a", INTERACTIVE, _square, 2)
    pool.start()
    assert pool.submit("a", INTERACTIVE, _square, 3).result(5) == 9
    pool.shutdown()


def test_layout_model_threads_limited():
    """版面分析模型在导入 pymupdf 时就创建 ONNX 会话，线程限制必须在那之前生效。"""
    import subprocess

    code = (
        "import gc, onnxruntime as ort\n"
        "from app import extract\n"
        "base = getattr(ort.InferenceSession, '_stratum_base', ort.InferenceSession)\n"
        "extract.extract_pages('samples/text_cn.pdf', [0], 'off', 200)\n"
        "n = [o.get_session_options().intra_op_num_threads for o in gc.get_objects() if isinstance(o, base)]\n"
        "assert n and all(x == 3 for x in n), n\n"
    )
    env = {**os.environ, "STRATUM_OCR_THREADS": "3", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]), env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
