"""Build the Windows program: a single portable exe, or a folder for the installer.

The build bundles its own copy of ffmpeg and the Qt libraries, so it runs on PCs with nothing else installed.

    python -m venv .venv
    .venv\\Scripts\\pip install -r requirements-build.lock      (or requirements-build.txt for the newest versions)
    .venv\\Scripts\\python build_exe.py                          the portable exe: dist/Free Video Editor.exe
    .venv\\Scripts\\python build_exe.py --onedir                 a folder: dist/Free Video Editor/ (opens faster)
    .venv\\Scripts\\python build_exe.py --onedir --installer     the folder, then dist/FreeVideoEditor-Setup-<version>.exe

The single-file exe unpacks itself to a temporary folder every time it starts (hundreds of MB, several seconds).
The installer therefore packs the folder build instead: it is unpacked once, when the program is installed.

Set FFMPEG_PATH to bundle a specific ffmpeg.exe instead of the one from imageio-ffmpeg (the build then only
warns that licenses/FFMPEG-NOTICE.txt may not describe it). Inno Setup 6 (ISCC.exe on PATH, in its usual install
folder, or named by the ISCC environment variable) is needed for --installer. --installer-only packages a folder
that is already in dist/. --verify <exe> runs the speech-engine self-test on a built exe and checks the sizes.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from video_converter import __version__

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP_NAME = "Free Video Editor"
AUTHOR = "Ahmed Ragab"
EXE_NAME = f"{APP_NAME}.exe"
ONEFILE_EXE = DIST / EXE_NAME                  # --onefile (the default): the portable exe
ONEDIR_FOLDER = DIST / APP_NAME                # --onedir: the folder the installer packs
ONEDIR_EXE = ONEDIR_FOLDER / EXE_NAME
NOTICE = ROOT / "licenses" / "FFMPEG-NOTICE.txt"

# What is expected of a build, in MB (a rough guard against a runaway or a broken bundle, not a promise): the
# portable exe, the whole folder, the installer. Tighten these once a few releases have shown the real figures.
SIZE_BOUNDS_MB = {
    "onefile": (60, 450),
    "onedir": (150, 750),
    "installer": (60, 450),
}

# faster-whisper and what it loads at run time: ctranslate2 (the engine and its DLLs), onnxruntime and the
# bundled voice-detector model, and the tokenizers.
SPEECH_PACKAGES = ("faster_whisper", "ctranslate2", "onnxruntime", "tokenizers")
# Packages that onnxruntime / huggingface_hub can import when they are installed but that captions never use.
# "av" (PyAV) is left out on purpose: the app extracts the sound with ffmpeg.exe and hands numbers to the speech
# engine, so PyAV is never called; its wheels carry FFmpeg libraries plus the GPL codecs x264 and x265, which the
# program should not ship (see AV_STUB below). hf_xet is huggingface_hub's optional fast downloader; the app
# downloads the models with its own code.
SPEECH_EXCLUDES = ("torch", "tensorflow", "sympy", "matplotlib", "pandas", "scipy", "IPython", "onnx",
                   "tensorboard", "sklearn", "numba", "pytest", "av", "hf_xet")
# Qt modules the app does not use (it imports QtCore, QtGui, QtWidgets, QtMultimedia and QtSvg only).
QT_EXCLUDES = ("QtQml", "QtQuick", "QtQuickWidgets", "QtQuickControls2", "QtQuick3D", "QtVirtualKeyboard",
               "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebView", "Qt3DCore", "Qt3DRender",
               "Qt3DInput", "Qt3DLogic", "Qt3DAnimation", "Qt3DExtras", "QtCharts", "QtDataVisualization",
               "QtGraphs", "QtGraphsWidgets", "QtDesigner", "QtHelp", "QtPdf", "QtPdfWidgets", "QtSql",
               "QtLocation", "QtPositioning", "QtSensors", "QtNfc", "QtBluetooth", "QtSerialPort", "QtSerialBus",
               "QtRemoteObjects", "QtScxml", "QtStateMachine", "QtTextToSpeech", "QtWebChannel", "QtWebSockets",
               "QtHttpServer", "QtSpatialAudio", "QtTest", "QtUiTools", "QtOpenGLWidgets", "QtDBus", "QtXml",
               "QtConcurrent", "QtPrintSupport", "QtNetworkAuth", "QtAxContainer",
               "QtMultimediaWidgets", "QtSvgWidgets")
# Files taken out of the bundle after PyInstaller has collected it. Names are matched in lower case.
DROP_BINARY_PREFIXES = ("qt6quick", "qt6qml", "qt6virtualkeyboard", "qt6pdf", "qt63d", "qt6webengine",
                        "qt6designer", "qt6charts", "qt6datavisualization", "qt6graphs", "qt6location",
                        "qt6positioning", "qt6sensors", "qt6sql", "qt6labs", "qt6test",
                        "qt6help", "qt6remoteobjects", "qt6scxml", "qt6statemachine", "qt6texttospeech",
                        "qt6webchannel", "qt6websockets", "qt6bluetooth", "qt6nfc", "qt6serial", "qt6httpserver")
DROP_BINARY_NAMES = {
    "opengl32sw.dll",     # Qt's software OpenGL (20 MB); only Qt Quick on a PC without a GPU driver would use it
    "cudnn64_9.dll",      # NVIDIA's cuDNN stub from the CTranslate2 wheel; the captions run on the CPU
}
DROP_DATA_PREFIXES = ("pyside6/translations/", "pyside6/qt/translations/")   # Qt's own translations: the app never loads them (no QTranslator)

# A stand-in for PyAV, shipped as plain data (see SPEECH_EXCLUDES). faster_whisper.audio does "import av" at the top
# but only uses it inside decode_audio(), which the app never calls (it passes a numpy array).
AV_STUB = '''"""Stand-in for PyAV in Free Video Editor's Windows builds.

faster-whisper imports "av" when it loads but only uses it to decode audio files itself. This program extracts
the sound with ffmpeg.exe and passes the samples in, so the real PyAV (and the FFmpeg and GPL codec libraries
that come with it) is left out. See licenses/SPEECH-NOTICE.txt.
"""

__version__ = "0.0.0+stub"


def open(*args, **kwargs):
    raise RuntimeError("PyAV is not included in Free Video Editor; audio is decoded with ffmpeg.exe instead.")
'''


# ----- ffmpeg ------------------------------------------------------------------------

def locate_ffmpeg() -> Path:
    if os.environ.get("FFMPEG_PATH"):
        return Path(os.environ["FFMPEG_PATH"])
    try:
        import imageio_ffmpeg
    except ImportError:
        sys.exit("imageio-ffmpeg is not installed. Run: pip install -r requirements-build.txt")
    return Path(imageio_ffmpeg.get_ffmpeg_exe())


def ffmpeg_report(ffmpeg: Path) -> tuple[str, str]:
    """(version, configure line) as printed by `ffmpeg -version`."""
    out = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True, check=True, timeout=60).stdout
    version = re.search(r"^ffmpeg version (\S+)", out, re.M)
    config = re.search(r"^configuration: (.*)$", out, re.M)
    if not version or not config:
        sys.exit(f"Could not read the version of {ffmpeg} from `ffmpeg -version`:\n{out}")
    return version.group(1), config.group(1)


def check_ffmpeg_notice(ffmpeg: Path) -> None:
    """Fail unless licenses/FFMPEG-NOTICE.txt describes this ffmpeg.exe (same version, every configure option
    mentioned). The notice carries the GPL source offer, so it has to stay true when the ffmpeg build changes."""
    notice = NOTICE.read_text(encoding="utf-8")
    declared = re.search(r"^Bundled FFmpeg version: (\S+)\s*$", notice, re.M)
    if not declared:
        sys.exit(f'{NOTICE} has no "Bundled FFmpeg version: ..." line.')
    version, config = ffmpeg_report(ffmpeg)
    problems = []
    if version != declared.group(1):
        problems.append(f"the bundled ffmpeg is {version} but the notice says {declared.group(1)}")
    missing = sorted(set(re.findall(r"--[\w-]+(?:=\S+)?", config)) - set(re.findall(r"--[\w-]+(?:=\S+)?", notice)))
    if missing:
        problems.append("these configure options are not in the notice: " + " ".join(missing))
    if "--enable-gpl" not in config.split():
        problems.append("the bundled ffmpeg is no longer a GPL build; the notice says it is")
    if not problems:
        print(f"ffmpeg {version} matches {NOTICE.name}")
        return
    message = ("licenses/FFMPEG-NOTICE.txt does not match the ffmpeg that would be bundled:\n  - "
               + "\n  - ".join(problems)
               + "\nUpdate the notice (version, configure line, libraries, source links) before building.")
    if os.environ.get("FFMPEG_PATH"):
        print(f"WARNING (FFMPEG_PATH is set): {message}", file=sys.stderr)
    else:
        sys.exit(message)


# ----- the PyInstaller build ----------------------------------------------------------

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


def trim_bundle(binaries, datas, manifest: str):
    """Called from the generated .spec file: drop what the app never uses and write the list of what is left."""
    def name_of(entry) -> str:
        return str(entry[0]).replace("\\", "/")

    def drop_binary(entry) -> bool:
        base = name_of(entry).rsplit("/", 1)[-1].lower()
        return base in DROP_BINARY_NAMES or base.startswith(DROP_BINARY_PREFIXES)

    def drop_data(entry) -> bool:
        return name_of(entry).lower().startswith(DROP_DATA_PREFIXES)

    kept_binaries = [entry for entry in binaries if not drop_binary(entry)]
    kept_datas = [entry for entry in datas if not drop_data(entry)]
    dropped = len(binaries) - len(kept_binaries), len(datas) - len(kept_datas)
    print(f"trim_bundle: dropped {dropped[0]} libraries and {dropped[1]} data files")
    Path(manifest).write_text("\n".join(sorted(name_of(e) for e in [*kept_binaries, *kept_datas])) + "\n",
                              encoding="utf-8")
    return kept_binaries, kept_datas


def pyinstaller_options(mode: str, staging: Path, version_file: Path) -> list[str]:
    sep = os.pathsep
    return [
        f"--{mode}", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ROOT / "assets" / "icon.ico"),
        "--version-file", str(version_file),
        "--add-data", f"{ROOT / 'assets' / 'icon.ico'}{sep}assets",
        "--add-binary", f"{staging / 'ffmpeg' / 'ffmpeg.exe'}{sep}ffmpeg",
        "--add-data", f"{staging / 'ffmpeg' / 'NOTICE.txt'}{sep}ffmpeg",
        "--add-data", f"{ROOT / 'licenses'}{sep}licenses",
        # the translations (video_converter/locales/*.json) are read as files at run time
        "--add-data", f"{ROOT / 'video_converter' / 'locales'}{sep}video_converter/locales",
        "--add-data", f"{ROOT / 'LICENSE'}{sep}.",
        # the stand-in for PyAV, importable from the bundle folder
        "--add-data", f"{staging / 'stub' / 'av'}{sep}av",
        # ffmpeg is bundled directly; don't pull in imageio-ffmpeg's copy as well.
        "--exclude-module", "imageio_ffmpeg",
        "--exclude-module", "tkinter",
        *[arg for module in QT_EXCLUDES for arg in ("--exclude-module", f"PySide6.{module}")],
        # The speech engine for automatic captions (the models themselves are downloaded by the app, not bundled).
        *[arg for package in SPEECH_PACKAGES for arg in ("--collect-all", package)],
        *[arg for module in SPEECH_EXCLUDES for arg in ("--exclude-module", module)],
        "--specpath", str(BUILD / f"spec-{mode}"),
        str(ROOT / "Free Video Editor.pyw"),
    ]


def make_spec(mode: str, staging: Path, version_file: Path, manifest: Path) -> Path:
    """Have PyInstaller write the .spec file, then add the trimming step to it."""
    spec_dir = BUILD / f"spec-{mode}"
    shutil.rmtree(spec_dir, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "PyInstaller.utils.cliutils.makespec",
                    *pyinstaller_options(mode, staging, version_file)], check=True)
    spec = spec_dir / f"{APP_NAME}.spec"
    text = spec.read_text(encoding="utf-8")
    marker = "pyz = PYZ(a.pure"
    if text.count(marker) != 1:
        sys.exit(f"Unexpected layout of {spec}: could not find where to add the trimming step.")
    step = (
        "# --- added by build_exe.py: leave out what the app never uses ---\n"
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "import build_exe\n"
        f"a.binaries, a.datas = build_exe.trim_bundle(a.binaries, a.datas, {str(manifest)!r})\n\n"
    )
    spec.write_text(text.replace(marker, step + marker), encoding="utf-8")
    return spec


def build_exe(mode: str, spec_only: bool = False) -> Path:
    """Build the single-file exe (mode "onefile") or the folder (mode "onedir"); returns the exe."""
    ffmpeg = locate_ffmpeg()
    if not ffmpeg.is_file():
        sys.exit(f"ffmpeg not found at {ffmpeg}")
    check_ffmpeg_notice(ffmpeg)

    staging = BUILD / f"bundle-{mode}"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "ffmpeg").mkdir(parents=True)
    (staging / "stub" / "av").mkdir(parents=True)
    shutil.copy2(ffmpeg, staging / "ffmpeg" / "ffmpeg.exe")
    shutil.copy2(NOTICE, staging / "ffmpeg" / "NOTICE.txt")
    (staging / "stub" / "av" / "__init__.py").write_text(AV_STUB, encoding="utf-8")
    version_file = BUILD / "version_info.txt"
    write_version_file(version_file)
    manifest = BUILD / f"bundle-contents-{mode}.txt"

    spec = make_spec(mode, staging, version_file, manifest)
    if spec_only:
        print(f"Wrote {spec}")
        return spec
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD / f"pyinstaller-{mode}"), str(spec)],
        check=True,
    )
    check_manifest(manifest)
    exe = ONEFILE_EXE if mode == "onefile" else ONEDIR_EXE
    if not exe.is_file():
        sys.exit(f"PyInstaller finished but {exe} was not created.")
    print(f"\nBuilt {exe} ({exe.stat().st_size / 1e6:.0f} MB)" +
          (f"; the folder is {folder_size(ONEDIR_FOLDER) / 1e6:.0f} MB" if mode == "onedir" else ""))
    return exe


# Names that must not be in a build: PyAV and its GPL codecs, the optional downloader, NVIDIA's cuDNN stub.
FORBIDDEN = re.compile(r"(^|/)(av\.libs/|libx26[45]|hf_xet|avcodec-6[0-2]|cudnn64|opengl32sw)", re.I)


def check_manifest(manifest: Path) -> None:
    names = manifest.read_text(encoding="utf-8").splitlines()
    bad = [n for n in names if FORBIDDEN.search(n)]
    if bad:
        sys.exit("These files should not be in the build:\n  " + "\n  ".join(bad))
    if not any(n.endswith("ffmpeg/ffmpeg.exe") for n in names):
        sys.exit("ffmpeg.exe is not in the build.")
    print(f"{len(names)} files in the bundle (the list is in {manifest})")


def folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


# ----- checking a build ---------------------------------------------------------------

def check_size(kind: str, size_bytes: int) -> None:
    low, high = SIZE_BOUNDS_MB[kind]
    megabytes = size_bytes / 1e6
    print(f"{kind}: {megabytes:.0f} MB (expected {low} to {high} MB)")
    if not low <= megabytes <= high:
        sys.exit(f"The {kind} build is {megabytes:.0f} MB, outside the expected {low} to {high} MB.")


def verify(path: Path) -> None:
    """Run `--self-test-captions` on a built exe (the speech engine must load; the report must start with "ok")
    and check the sizes. `path` is the exe, or the onedir folder."""
    exe = path / EXE_NAME if path.is_dir() else path
    kind = "onedir" if path.is_dir() or exe.parent.name == APP_NAME else "onefile"
    if not exe.is_file():
        sys.exit(f"{exe} not found.")
    check_size(kind, folder_size(exe.parent) if kind == "onedir" else exe.stat().st_size)
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "captions-self-test.txt"
        done = subprocess.run([str(exe), "--self-test-captions", str(report)], timeout=300)
        text = report.read_text(encoding="utf-8", errors="replace") if report.is_file() else ""
    print(f"{exe.name} --self-test-captions: exit code {done.returncode}\n{text.strip()}")
    if done.returncode != 0 or not text.startswith("ok"):
        sys.exit("The speech-engine self-test failed.")


# ----- the installer ------------------------------------------------------------------

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
    if not ONEDIR_EXE.is_file():
        sys.exit(f"{ONEDIR_EXE} not found; build the folder first (python build_exe.py --onedir --installer).")
    iscc = locate_iscc()
    if not iscc:
        sys.exit("Inno Setup 6 (ISCC.exe) not found. Install it from https://jrsoftware.org/isdl.php "
                 "(or: choco install innosetup), or set the ISCC environment variable to ISCC.exe.")
    subprocess.run([str(iscc), "/Qp", f"/DAppVersion={__version__}", str(ROOT / "installer" / "FreeVideoEditor.iss")],
                   check=True)
    setup = DIST / f"FreeVideoEditor-Setup-{__version__}.exe"
    if not setup.is_file():
        sys.exit(f"Inno Setup finished but {setup} was not created.")
    print(f"Built {setup} ({setup.stat().st_size / 1e6:.0f} MB)")
    return setup


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} for Windows.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--onefile", action="store_true",
                      help="build the portable single-file exe, dist/Free Video Editor.exe (the default)")
    mode.add_argument("--onedir", action="store_true",
                      help="build a folder, dist/Free Video Editor/, that opens much faster than the single file")
    parser.add_argument("--installer", action="store_true",
                        help="also build the Inno Setup installer from the folder build (implies --onedir)")
    parser.add_argument("--installer-only", action="store_true",
                        help="only build the installer from the folder already in dist/")
    parser.add_argument("--verify", metavar="EXE_OR_FOLDER", type=Path,
                        help="run the captions self-test on a built exe (or folder) and check its size, then stop")
    parser.add_argument("--check-ffmpeg", action="store_true",
                        help="only check that licenses/FFMPEG-NOTICE.txt describes the ffmpeg that would be bundled")
    parser.add_argument("--spec-only", action="store_true",
                        help="only write the PyInstaller .spec file under build/ (a quick check of the options)")
    args = parser.parse_args()
    if sys.platform != "win32":
        sys.exit("build_exe.py builds a Windows .exe; run it on Windows.")
    if args.verify:
        verify(args.verify)
        return
    if args.check_ffmpeg:
        ffmpeg = locate_ffmpeg()
        if not ffmpeg.is_file():
            sys.exit(f"ffmpeg not found at {ffmpeg}")
        check_ffmpeg_notice(ffmpeg)
        return
    if args.installer_only:
        build_installer()
        return
    build_exe("onedir" if args.onedir or args.installer else "onefile", spec_only=args.spec_only)
    if args.installer and not args.spec_only:
        setup = build_installer()
        check_size("installer", setup.stat().st_size)


if __name__ == "__main__":
    main()
