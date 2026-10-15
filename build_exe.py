"""Build a standalone Windows executable: dist/Video Converter.exe

The exe bundles its own copy of ffmpeg, so it runs on PCs without ffmpeg installed.

    python -m venv .venv
    .venv\\Scripts\\pip install -r requirements-build.txt
    .venv\\Scripts\\python build_exe.py

Set FFMPEG_PATH to bundle a specific ffmpeg.exe instead of the one from imageio-ffmpeg.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from mp4_to_mp3 import __version__

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
APP_NAME = "Video Converter"
AUTHOR = "Ahmed Ragab"


def locate_ffmpeg() -> Path:
    if os.environ.get("FFMPEG_PATH"):
        return Path(os.environ["FFMPEG_PATH"])
    try:
        import imageio_ffmpeg
    except ImportError:
        sys.exit("imageio-ffmpeg is not installed. Run: pip install -r requirements-build.txt")
    return Path(imageio_ffmpeg.get_ffmpeg_exe())


def write_version_file(path: Path) -> None:
    parts = [int(p) for p in __version__.split(".")] + [0]
    numbers = tuple(parts[:4])
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{AUTHOR}'),
      StringStruct('FileDescription', '{APP_NAME}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 {AUTHOR}. MIT License.'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def main() -> None:
    if sys.platform != "win32":
        sys.exit("build_exe.py builds a Windows .exe; run it on Windows.")

    ffmpeg = locate_ffmpeg()
    if not ffmpeg.is_file():
        sys.exit(f"ffmpeg not found at {ffmpeg}")

    staging = BUILD / "bundle"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "ffmpeg").mkdir(parents=True)
    shutil.copy2(ffmpeg, staging / "ffmpeg" / "ffmpeg.exe")
    shutil.copy2(ROOT / "licenses" / "FFMPEG-NOTICE.txt", staging / "ffmpeg" / "NOTICE.txt")
    version_file = BUILD / "version_info.txt"
    write_version_file(version_file)

    sep = os.pathsep
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile", "--windowed",
            "--name", APP_NAME,
            "--icon", str(ROOT / "assets" / "icon.ico"),
            "--version-file", str(version_file),
            "--add-data", f"{ROOT / 'assets' / 'icon.ico'}{sep}assets",
            "--add-binary", f"{staging / 'ffmpeg' / 'ffmpeg.exe'}{sep}ffmpeg",
            "--add-data", f"{staging / 'ffmpeg' / 'NOTICE.txt'}{sep}ffmpeg",
            # ffmpeg is bundled directly; don't pull in imageio-ffmpeg's copy as well.
            "--exclude-module", "imageio_ffmpeg",
            "--distpath", str(ROOT / "dist"),
            "--workpath", str(BUILD / "pyinstaller"),
            "--specpath", str(BUILD),
            str(ROOT / "Video Converter.pyw"),
        ],
        check=True,
    )
    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    print(f"\nBuilt {exe} ({exe.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
