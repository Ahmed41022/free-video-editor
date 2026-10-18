"""Build a standalone Windows executable: dist/Free Video Editor.exe

The exe bundles its own copy of ffmpeg and the Qt libraries, so it runs on PCs with nothing else installed.

    python -m venv .venv
    .venv\\Scripts\\pip install -r requirements-build.txt
    .venv\\Scripts\\python build_exe.py

Set FFMPEG_PATH to bundle a specific ffmpeg.exe instead of the one from imageio-ffmpeg.

Add --installer to also build dist/FreeVideoEditor-Setup-<version>.exe with Inno Setup 6 (ISCC.exe on PATH, in
its usual install folder, or named by the ISCC environment variable). Use --installer-only to skip the exe
build and package an exe that is already in dist/.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from video_converter import __version__

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
APP_NAME = "Free Video Editor"
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


def locate_iscc() -> Path | None:
    """Find Inno Setup's command-line compiler, or None when Inno Setup isn't installed."""
    if os.environ.get("ISCC"):
        path = Path(os.environ["ISCC"])
        return path if path.is_file() else None
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        for folder in (Path(base) / "Inno Setup 6", Path(base) / "Programs" / "Inno Setup 6"):
            if (folder / "ISCC.exe").is_file():
                return folder / "ISCC.exe"
    return None


def build_installer() -> Path:
    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    if not exe.is_file():
        sys.exit(f"{exe} not found; build the exe first (run without --installer-only).")
    iscc = locate_iscc()
    if not iscc:
        sys.exit("Inno Setup 6 (ISCC.exe) not found. Install it from https://jrsoftware.org/isdl.php "
                 "(or: choco install innosetup), or set the ISCC environment variable to ISCC.exe.")
    subprocess.run([str(iscc), "/Qp", f"/DAppVersion={__version__}", str(ROOT / "installer" / "FreeVideoEditor.iss")],
                   check=True)
    setup = ROOT / "dist" / f"FreeVideoEditor-Setup-{__version__}.exe"
    if not setup.is_file():
        sys.exit(f"Inno Setup finished but {setup} was not created.")
    print(f"Built {setup} ({setup.stat().st_size / 1e6:.0f} MB)")
    return setup


def build_exe() -> None:
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
            "--add-data", f"{ROOT / 'licenses'}{sep}licenses",
            "--add-data", f"{ROOT / 'LICENSE'}{sep}.",
            # ffmpeg is bundled directly; don't pull in imageio-ffmpeg's copy as well.
            "--exclude-module", "imageio_ffmpeg",
            "--exclude-module", "tkinter",
            "--distpath", str(ROOT / "dist"),
            "--workpath", str(BUILD / "pyinstaller"),
            "--specpath", str(BUILD),
            str(ROOT / "Free Video Editor.pyw"),
        ],
        check=True,
    )
    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    print(f"\nBuilt {exe} ({exe.stat().st_size / 1e6:.0f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} for Windows.")
    parser.add_argument("--installer", action="store_true",
                        help="also build the Inno Setup installer (needs Inno Setup 6)")
    parser.add_argument("--installer-only", action="store_true",
                        help="only build the installer from the exe already in dist/")
    args = parser.parse_args()
    if sys.platform != "win32":
        sys.exit("build_exe.py builds a Windows .exe; run it on Windows.")
    if not args.installer_only:
        build_exe()
    if args.installer or args.installer_only:
        build_installer()


if __name__ == "__main__":
    main()
