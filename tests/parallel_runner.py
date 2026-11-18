"""Run the test suite on several processes at once: `python -m tests` (tests/__main__.py calls main() here).

It finds exactly the tests `python -m unittest discover -s tests` finds, and runs each test class as a whole in
one worker process, so a class's setUpClass fixture still runs once. Every worker is its own process, so it has
its own QApplication, its own temporary folder (TMP/TEMP/TMPDIR) and its own user name, which the app's single
instance socket and lock file are named after. The output is like unittest's: the failures with their
tracebacks, a line "Ran N tests in S", then OK or FAILED; the exit code is 1 for any failure, error or worker
that died.

    python -m tests                          every test, on as many workers as there are CPUs (at most 8)
    python -m tests -j 4 -v                  four workers, one line per test
    python -m tests test_phone test_video.VideoExportTests    only these modules, classes or tests
    python -m tests --list                   the ids of the tests it would run, one per line
    python -m tests --durations 15           and the slowest classes and tests, measured

A test that runs longer than --timeout seconds (900 by default) gets every thread's stack written to the
error output and its worker ended, which fails the run: a hung test cannot stall it for ever. Only the
standard library is used.
"""
import argparse
import faulthandler
import json
import multiprocessing
import os
import queue
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TIMES_FILE = HERE / "class_times.json"       # seconds each class took last time: the slowest are started first
MAX_WORKERS = 8
DEFAULT_TIMEOUT = 900


def class_key(test) -> str:
    cls = type(test)
    return f"{cls.__module__}.{cls.__qualname__}"


def discover() -> unittest.TestSuite:
    """The suite `unittest discover -s tests` builds (the tests folder is also the top level directory)."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    return unittest.TestLoader().discover(str(HERE))


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def selected(tests, targets):
    """The tests whose id is one of `targets` or lies below one (a module, a class, a test)."""
    if not targets:
        return list(tests)
    names = [t[len("tests."):] if t.startswith("tests.") else t for t in targets]
    return [t for t in tests if any(t.id() == n or t.id().startswith(n + ".") for n in names)]


def plan(tests):
    """{class key: [test ids]} in the order discover runs them. Tests that failed to import are left out
    (they are run in the main process, where they simply report their error)."""
    groups: dict[str, list[str]] = {}
    broken = []
    for test in tests:
        if type(test).__module__ == "unittest.loader":
            broken.append(test)
        else:
            groups.setdefault(class_key(test), []).append(test.id())
    return groups, broken


# ----- a worker ---------------------------------------------------------------------------------------

class Recorder(unittest.TestResult):
    """A result that keeps what happened, as plain data the main process can print."""

    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout
        self.rows = []              # (test id, description, status, seconds)
        self._started = 0.0
        self._rowed = False
        self._subtest_status = None

    def startTest(self, test):
        super().startTest(test)
        self._started = time.perf_counter()
        self._rowed = False
        self._subtest_status = None
        faulthandler.dump_traceback_later(self.timeout, exit=True)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        if not self._rowed:         # a test whose only news was from its sub-tests
            self._row(test, self._subtest_status or "ok")
        super().stopTest(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None and self._subtest_status != "ERROR":
            self._subtest_status = "FAIL" if issubclass(err[0], test.failureException) else "ERROR"

    def _row(self, test, status):
        if type(test).__name__ == "_ErrorHolder":       # setUpClass and the like: not a test of the suite
            return
        self._rowed = True
        self.rows.append((test.id(), str(test), status, time.perf_counter() - self._started))

    def addSuccess(self, test):
        super().addSuccess(test)
        self._row(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._row(test, "FAIL")

    def addError(self, test, err):
        super().addError(test, err)
        self._row(test, "ERROR")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._row(test, "skipped")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._row(test, "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._row(test, "unexpected success")


def run_group(ids, timeout):
    """Run the tests `ids` (those of one class) and return what happened."""
    began = time.perf_counter()
    result = Recorder(timeout)
    faulthandler.dump_traceback_later(timeout, exit=True)       # a hang in setUpClass counts too
    try:
        suite = unittest.TestLoader().loadTestsFromNames(ids)
        suite.run(result)
    finally:
        faulthandler.cancel_dump_traceback_later()
    return {
        "seconds": time.perf_counter() - began,
        "ran": result.testsRun,
        "rows": result.rows,
        "failures": [(str(t), text) for t, text in result.failures],
        "errors": [(str(t), text) for t, text in result.errors],
        "skipped": [(str(t), reason) for t, reason in result.skipped],
        "expected": len(result.expectedFailures),
        "unexpected": [str(t) for t in result.unexpectedSuccesses],
    }


def worker(number, tasks, results, timeout, base):
    folder = Path(base) / f"worker-{number}"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(folder)
    tempfile.tempdir = None
    # The app names its single-instance socket and lock after the user: one per worker.
    for name in ("LOGNAME", "USER", "USERNAME"):
        os.environ[name] = f"fve-test-{os.getpid()}"
    os.environ["FVE_TEST_WORKER"] = str(number)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(HERE))
    faulthandler.enable()
    while True:
        task = tasks.get()
        if task is None:
            return
        key, ids = task
        results.put(("start", number, key))
        try:
            outcome = run_group(ids, timeout)
        except BaseException as error:              # not a test failing: the runner could not run the class
            outcome = {"crash": f"{type(error).__name__}: {error}"}
        results.put(("done", number, key, outcome))


# ----- the main process -------------------------------------------------------------------------------

def load_times():
    try:
        return json.loads(TIMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run_all(groups, workers, timeout, verbose, quiet, out, in_order=False):
    """Run the groups on `workers` processes. Returns ({key: outcome}, [crash messages])."""
    times = load_times()
    # Slowest first; classes never seen before are treated as slow, so none of them ends the run alone.
    order = list(groups) if in_order else sorted(groups, key=lambda k: -times.get(k, 1e9))
    context = multiprocessing.get_context("spawn")
    tasks, results = context.Queue(), context.Queue()
    for key in order:
        tasks.put((key, groups[key]))
    base = tempfile.mkdtemp(prefix="fve-tests-")
    count = max(1, min(workers, len(order)))
    for _ in range(count):
        tasks.put(None)
    procs = {}

    def start(number):
        proc = context.Process(target=worker, args=(number, tasks, results, timeout, base), daemon=True)
        proc.start()
        procs[number] = proc

    for number in range(count):
        start(number)
    running = {}                       # worker number -> class key it is on
    outcomes, crashes = {}, []
    try:
        def handle(message):
            if message[0] == "start":
                running[message[1]] = message[2]
                return
            _, number, key, outcome = message
            running.pop(number, None)
            outcomes[key] = outcome
            if "crash" in outcome:
                crashes.append(f"{key}: {outcome['crash']}")
            elif not quiet:
                show(outcome, verbose, out)

        while len(outcomes) < len(order):
            # Look at who has ended first, then read every message: whatever a worker sent before it ended is
            # already in the queue by then.
            ended = [n for n, p in procs.items() if not p.is_alive()]
            try:
                handle(results.get(timeout=2))
                while True:
                    handle(results.get(timeout=0.2 if ended else 0))
            except queue.Empty:
                pass
            for number in ended:
                proc = procs.pop(number)
                if number in running:      # it died in the middle of a class: that class has not finished
                    key = running.pop(number)
                    crashes.append(f"{key}: the worker process ended with exit code {proc.exitcode} before the "
                                   f"class finished (a hang the guard ended, or a crash; see the stack above)")
                    outcomes[key] = {"crash": "worker died"}
                    if len(outcomes) + len(running) < len(order) and not tasks.empty():
                        start(number)
                elif proc.exitcode != 0 and len(outcomes) < len(order):
                    crashes.append(f"a worker process ended with exit code {proc.exitcode}")
            if not procs and len(outcomes) < len(order):
                for key in order:
                    if key not in outcomes:
                        outcomes[key] = {"crash": "never ran: all workers ended"}
                        crashes.append(f"{key}: never ran, all workers had ended")
    finally:
        for proc in procs.values():
            proc.join(timeout=5)
        for proc in procs.values():
            if proc.is_alive():
                proc.terminate()
        shutil.rmtree(base, ignore_errors=True)
    return outcomes, crashes


def show(outcome, verbose, out):
    if verbose:
        for _, description, status, _seconds in outcome["rows"]:
            print(f"{description} ... {status}", file=out)
    else:
        marks = {"ok": ".", "FAIL": "F", "ERROR": "E", "skipped": "s", "expected failure": "x",
                 "unexpected success": "u"}
        print("".join(marks[row[2]] for row in outcome["rows"]), end="", file=out)
    out.flush()


def report(outcomes, crashes, started, expected_ids, durations, out):
    failures = errors = skipped = ran = 0
    ids_run = []
    lines = []
    for key in sorted(outcomes):
        outcome = outcomes[key]
        if "rows" not in outcome:
            continue
        ran += outcome["ran"]
        skipped += len(outcome["skipped"])
        ids_run += [row[0] for row in outcome["rows"]]
        for kind, label in (("errors", "ERROR"), ("failures", "FAIL")):
            for description, text in outcome[kind]:
                lines.append(f"{'=' * 70}\n{label}: {description}\n{'-' * 70}\n{text}")
        for description in outcome["unexpected"]:
            lines.append(f"{'=' * 70}\nUNEXPECTED SUCCESS: {description}")
        failures += len(outcome["failures"])
        errors += len(outcome["errors"])
    for crash in crashes:
        lines.append(f"{'=' * 70}\nERROR: {crash}")
    errors += len(crashes)
    # The runner's own check: the tests that ran are exactly the ones discovered (setUpClass errors add rows
    # that are not tests, so only the missing and the unknown ones are compared).
    missing = sorted(set(expected_ids) - set(ids_run)) if not crashes else []
    extra = sorted(set(ids_run) - set(expected_ids))
    if missing or extra:
        lines.append(f"{'=' * 70}\nERROR: the tests that ran are not the tests that were found\n"
                     f"  not run: {missing[:20]}\n  not found: {extra[:20]}")
        errors += 1
    elapsed = time.perf_counter() - started
    print(file=out)
    for line in lines:
        print(line, file=out)
    print("-" * 70, file=out)
    print(f"Ran {ran} test{'s' if ran != 1 else ''} in {elapsed:.3f}s\n", file=out)
    ok = not (failures or errors or any(outcome.get("unexpected") for outcome in outcomes.values() if "rows" in outcome))
    extras = [f"{name}={n}" for name, n in (("failures", failures), ("errors", errors), ("skipped", skipped)) if n]
    if ok:
        print("OK" + (f" (skipped={skipped})" if skipped else ""), file=out)
    else:
        print(f"FAILED ({', '.join(extras)})", file=out)
    if durations:
        show_durations(outcomes, durations, out)
    return ok


def show_durations(outcomes, count, out):
    done = {k: o for k, o in outcomes.items() if "rows" in o}
    print(f"\nThe {count} slowest classes (all their tests, with setUpClass and the like):", file=out)
    for key, outcome in sorted(done.items(), key=lambda kv: -kv[1]["seconds"])[:count]:
        tests = sum(row[3] for row in outcome["rows"])
        print(f"  {outcome['seconds']:8.1f}s  (tests {tests:7.1f}s, fixtures {outcome['seconds'] - tests:6.1f}s, "
              f"{outcome['ran']:3d} tests)  {key}", file=out)
    rows = sorted((row for o in done.values() for row in o["rows"]), key=lambda r: -r[3])[:count]
    print(f"\nThe {count} slowest tests:", file=out)
    for test_id, _, _, seconds in rows:
        print(f"  {seconds:8.2f}s  {test_id}", file=out)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tests", description="Run the test suite in parallel.")
    parser.add_argument("targets", nargs="*", help="modules, classes or tests to run (default: all)")
    parser.add_argument("-j", "--workers", type=int, default=min(os.cpu_count() or 1, MAX_WORKERS),
                        help="how many worker processes (default: the number of CPUs, at most %d)" % MAX_WORKERS)
    parser.add_argument("-v", "--verbose", action="store_true", help="one line per test")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only the failures and the summary")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds a test may take before its worker is ended (default: %d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--list", action="store_true", help="print the ids of the tests and stop")
    parser.add_argument("--durations", type=int, metavar="N", help="also print the N slowest classes and tests")
    parser.add_argument("--in-order", action="store_true",
                        help="start the classes in the order discover runs them, not the slowest first")
    parser.add_argument("--save-times", action="store_true",
                        help="write how long each class took to tests/class_times.json (the order of the next runs)")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    tests = selected(list(flatten(discover())), args.targets)
    if args.list:
        print("\n".join(t.id() for t in tests))
        return 0
    if args.targets and not tests:
        print("No test matches " + ", ".join(args.targets), file=sys.stderr)
        return 1
    groups, broken = plan(tests)
    out = sys.stderr            # like unittest, which writes to the error output
    outcomes, crashes = run_all(groups, args.workers, args.timeout, args.verbose, args.quiet, out, args.in_order)
    for test in broken:         # an import that failed: run it here, it reports its error
        result = unittest.TestResult()
        test.run(result)
        outcomes[f"broken.{test.id()}"] = {
            "seconds": 0.0, "ran": result.testsRun, "rows": [(test.id(), str(test), "ERROR", 0.0)],
            "failures": [], "errors": [(str(t), x) for t, x in result.errors], "skipped": [], "unexpected": [],
        }
    ok = report(outcomes, crashes, started, [t.id() for t in tests if t not in broken] + [t.id() for t in broken],
                args.durations, out)
    if args.save_times and not crashes:
        times = {k: round(o["seconds"], 1) for k, o in outcomes.items() if "rows" in o and not k.startswith("broken.")}
        TIMES_FILE.write_text(json.dumps(dict(sorted(times.items())), indent=1) + "\n", encoding="utf-8")
    return 0 if ok else 1
