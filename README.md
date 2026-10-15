# Video Converter

A small Windows app that extracts the audio from MP4 videos and saves it as MP3.
Everything runs locally on your computer; no files are uploaded anywhere.

## Download

Grab **`Video Converter.exe`** from the [latest release](https://github.com/Ahmed41022/video-converter/releases/latest) and double-click it.
Nothing else to install: ffmpeg is built into the exe.

> Windows SmartScreen may warn about an unrecognized app the first time because the exe isn't code-signed.
> Click **More info → Run anyway**.

## Features

- Convert one file or a whole batch at once (add files or an entire folder)
- Drop videos onto `Video Converter.exe` to open them straight into the list
- Choose the quality: 128 / 192 / 256 / 320 kbps, or variable bitrate
- Save next to each video or into a folder of your choice
- Never overwrites existing files (`file.mp3` becomes `file (1).mp3`)
- Per-file and overall progress, with a Cancel button
- Keeps metadata such as title and artist when the video has it
- Also accepts MOV, MKV, AVI, WEBM, M4V and other common video formats

## How to use

1. Click **Add files…** or **Add folder…** (or drop videos onto the exe)
2. Pick a quality and where to save the MP3s
3. Click **Convert to MP3** (or press Enter)

## Running from source

Requires Python 3.9+ and ffmpeg (`winget install Gyan.FFmpeg`, `brew install ffmpeg`, `sudo apt install ffmpeg`,
or `pip install imageio-ffmpeg`). The app looks for ffmpeg in the `FFMPEG_PATH` environment variable, then on your `PATH`,
then in `imageio-ffmpeg`.

```bash
python -m mp4_to_mp3
```

On Windows you can also double-click **`Video Converter.pyw`**.

### Command line

Pass one or more files to convert them without opening the window:

```bash
python -m mp4_to_mp3 "My video.mp4" another.mp4 -q 320 -o converted
```

| Option | Description |
| --- | --- |
| `-q`, `--quality` | `128`, `192` (default), `256`, `320`, `vbr`, or `vbr-high` |
| `-o`, `--output-dir` | Folder for the MP3 files (default: next to each video) |
| `--overwrite` | Replace existing MP3 files instead of adding ` (1)` to the name |

## Building the exe

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-build.txt
.venv\Scripts\python build_exe.py
```

The result is `dist\Video Converter.exe`, a single file that includes its own copy of ffmpeg.

## Running the tests

```bash
python -m unittest discover -s tests -v
```

The conversion tests generate short sample videos with ffmpeg and are skipped if ffmpeg isn't installed.

## License

Video Converter is released under the [MIT License](LICENSE).
The Windows exe bundles FFmpeg, which is licensed under the GPLv3; see [licenses/FFMPEG-NOTICE.txt](licenses/FFMPEG-NOTICE.txt).
