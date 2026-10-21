import os

# The tests never touch the graphics card: a window skips its search for hardware encoders (the tests that check
# that search turn this off and replace the card with a stand-in).
os.environ.setdefault("VIDEO_EDITOR_NO_GPU_SEARCH", "1")
