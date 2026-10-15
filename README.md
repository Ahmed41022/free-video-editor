# MP4 to MP3 Converter

A small desktop app that extracts the audio from MP4 videos and saves it as MP3.
Everything runs locally on your computer with [ffmpeg](https://ffmpeg.org/); no files are uploaded anywhere.

## Features

- Convert one file or a whole batch at once (add files or an entire folder)
- Choose the quality: 128 / 192 / 256 / 320 kbps, or variable bitrate
- Save next to each video or into a folder of your choice
- Never overwrites existing files (`file.mp3` becomes `file (1).mp3`)
- Per-file and overall progress, with a Cancel button
- Keeps metadata such as title and artist when the video has it
- Also accepts MOV, MKV, AVI, WEBM, M4V and other common video formats
- Optional command-line mode for scripting

## Requirements

- Python 3.9 or newer (Tkinter is included with the standard Windows and macOS installers)
- ffmpeg, installed one of these ways:
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg` (or your distro's package manager)
  - Any platform: `pip install imageio-ffmpeg` (downloads a bundled ffmpeg binary)

The app looks for ffmpeg in the `FFMPEG_PATH` environment variable first, then on your `PATH`, then in `imageio-ffmpeg`.

## Usage

### Desktop app

```bash
python -m mp4_to_mp3
```

On Windows you can also double-click **`MP4 to MP3.pyw`** to open the app without a console window.

1. Click **Add files…** or **Add folder…**
2. Pick a quality and where to save the MP3s
3. Click **Convert to MP3**

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

## Running the tests

```bash
python -m unittest discover -s tests -v
```

The conversion tests generate short sample videos with ffmpeg and are skipped if ffmpeg isn't installed.

## License

[MIT](LICENSE)
