# Double-click this file on Windows to open the editor without a console window.
# Video files passed as arguments (or dropped onto the .exe) are added to the list.
import sys

from video_converter.ui.app import main

sys.exit(main(sys.argv[1:]))
