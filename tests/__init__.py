import os

# The tests never touch the graphics card: a window skips its search for hardware encoders (the
# tests that check that search turn this off and replace the card with a stand-in).
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")

# Closing a window with files open outside a project asks what to do with them; the tests that
# don't test that question never wait for an answer (tests/test_projects.py turns it back on).
os.environ.setdefault("VIDEO_EDITOR_NO_QUIT_PROMPT", "1")
# A window made without its own settings file would use the real projects folder: not in a test.
if "VIDEO_EDITOR_PROJECTS_DIR" not in os.environ:
    import atexit
    import shutil
    import tempfile

    _projects_dir = tempfile.mkdtemp(prefix="fve-test-projects-")
    os.environ["VIDEO_EDITOR_PROJECTS_DIR"] = _projects_dir
    atexit.register(shutil.rmtree, _projects_dir, True)
