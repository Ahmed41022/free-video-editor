# Video Converter

A small Windows app that turns videos into MP3 audio, or reshapes, crops and trims them into new MP4s,
for example to fit a phone screen for Stories, Reels, TikTok or Shorts.
Everything runs locally on your computer; no files are uploaded anywhere.

![Video Converter editing a landscape video into a 9:16 phone video](docs/screenshot.png)

## Download

Grab **`VideoConverter.exe`** from the [latest release](https://github.com/Ahmed41022/video-converter/releases/latest) and double-click it.
Nothing else to install: ffmpeg is built into the exe.

> Windows SmartScreen may warn about an unrecognized app the first time because the exe isn't code-signed.
> Click **More info → Run anyway**.

## Features

**MP3 audio**
- Choose the quality: 128 / 192 / 256 / 320 kbps, or variable bitrate
- Keeps metadata such as title and artist when the video has it

**Video editing (MP4)**
- Reshape for phones (9:16), square posts (1:1), portrait posts (4:5) or widescreen (16:9)
- Crop to fill the frame: drag the box on the preview to pick what to keep, and zoom in (mouse wheel works too)
- Or fit the whole video inside the new shape with a blurred background or black bars
- Rotate 90° / 180°, mirror, remove the sound, and pick the size (1080p, 720p, 480p) and quality
- Live preview of the original and the result
- Phone recordings that are stored sideways are detected and shown the right way up

**Both**
- Trim: keep only part of the video, e.g. from `0:15` to `1:02:30`
- Convert one file or a whole batch at once (add files or an entire folder)
- Drop videos onto `VideoConverter.exe` to open them straight into the list
- Save next to each video or into a folder of your choice
- Never overwrites anything: edited videos are saved as `name (phone).mp4`, `name (square).mp4`, …,
  and existing files get a number (`file.mp3` becomes `file (1).mp3`)
- Per-file and overall progress, with a Cancel button
- Accepts MP4, MOV, MKV, AVI, WEBM, M4V and other common video formats

## How to use

1. Click **Add files…** or **Add folder…** (or drop videos onto the exe)
2. Pick the **MP3 audio** tab to extract sound, or the **Edit video (MP4)** tab to reshape and crop
3. Optionally set a trim range and where to save
4. Click **Convert to MP3** / **Export video** (or press Enter)

In the video tab, the crop position and zoom are remembered per file, so you can frame each video in a batch
differently: select a file, drag its box, then select the next one. The other settings apply to every file.

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
python -m mp4_to_mp3 "My video.mp4" --video phone --fit blur --start 0:10 --end 0:40
```

| Option | Description |
| --- | --- |
| `-q`, `--quality` | MP3 quality: `128`, `192` (default), `256`, `320`, `vbr`, or `vbr-high` |
| `-o`, `--output-dir` | Folder for the results (default: next to each video) |
| `--start`, `--end` | Trim, e.g. `--start 0:15 --end 1:02:30` |
| `--overwrite` | Replace existing files instead of adding ` (1)` to the name |
| `--video` | Export an MP4 instead: `phone`, `square`, `portrait`, `widescreen`, or `original` |
| `--fit` | `crop` (default), `blur`, or `bars` |
| `--position X Y` | Crop position from 0 to 1 (default `0.5 0.5`, the center) |
| `--zoom` | Crop tighter, `1.0` to `3.0` |
| `--rotate` | `90`, `180`, or `270` degrees clockwise |
| `--mirror`, `--mute` | Flip left-right; remove the sound |
| `--size` | Short side in pixels: `1080`, `720`, or `480` (default: same as the source) |
| `--video-quality` | `high`, `balanced` (default), or `small` |

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
