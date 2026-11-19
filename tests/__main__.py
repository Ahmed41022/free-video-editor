"""`python -m tests`: the test suite on several processes (see parallel_runner.py, or `python -m
tests -h`)."""
import multiprocessing
import sys

from tests.parallel_runner import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
