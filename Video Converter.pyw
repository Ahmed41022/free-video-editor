# Double-click this file on Windows to open the converter without a console window.
# Video files passed as arguments (or dropped onto the .exe) are added to the list.
import sys

from mp4_to_mp3.gui import main

main(sys.argv[1:])
