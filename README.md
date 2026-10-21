# Free Video Editor

[![Tests](https://github.com/Ahmed41022/free-video-editor/actions/workflows/tests.yml/badge.svg)](https://github.com/Ahmed41022/free-video-editor/actions/workflows/tests.yml)

A Windows video editor that reshapes, crops, cuts and joins videos into new MP4s, for example to fit a phone
screen for Stories, Reels, TikTok or Shorts, changes their speed, adds fades, adjusts and steadies the picture,
and turns videos into MP3 audio.
Everything runs on your computer, and no files are ever uploaded. The app only goes online for an optional
once-a-day check for a newer version (you can turn it off) and, if you use automatic captions, a one-time
download of the speech model. "Report a problem" opens your web browser on the project's GitHub page.

![Free Video Editor editing a landscape video into a 9:16 phone video](docs/screenshot.png)

## Download

Get either file from the [latest release](https://github.com/Ahmed41022/free-video-editor/releases/latest):

- **`FreeVideoEditor-Setup-<version>.exe`**: the installer. It installs just for you (no administrator
  rights needed, or choose all users), adds a Start menu entry, optionally a desktop shortcut, and an
  "Open with Free Video Editor" entry for video and audio files. It doesn't change which app opens your files by
  default. Remove it from Windows **Settings → Apps**.
- **`FreeVideoEditor.exe`**: the portable version. Double-click it; nothing to install. It is one file that
  unpacks itself to a temporary folder each time it starts, so it takes a few seconds longer to open (the
  installer unpacks everything once, so the installed app opens faster).

Everything the app needs is built in either way. The installed program lists the licenses of the software
it includes in Start menu → Free Video Editor → **Licenses**; they are also in [licenses/](licenses/).

**Install with winget** (once the listing is approved in the Windows Package Manager):
`winget install AhmedRagab.FreeVideoEditor`. The manifests are in [packaging/winget](packaging/winget/README.md).

The website is also available [in Arabic](https://ahmed41022.github.io/free-video-editor/ar/) (الموقع بالعربية),
and so is the app: see **Arabic interface** below.

> Windows SmartScreen may warn about an unrecognized app the first time because the files aren't code-signed.
> Click **More info → Run anyway**.
> Code signing is being prepared; see [docs/code-signing.md](docs/code-signing.md).

## Features

**Editor**
- Video player with a timeline: thumbnails, audio waveform, and drag handles to trim
- Cut sections out of the middle: split at the playhead, remove the parts you don't want, and drag the edges
  of a cut on the timeline to adjust it (right-click the timeline for more). Playback skips the cut parts
- Remove silences with one click: find the pauses in a talk or commentary, see how much shorter the video gets, and turn them into cuts you can still adjust
- Join several files into one video, in list order: drag files in the list to reorder them
- Crop box right on the video: drag it to choose what to keep, drag a corner (or scroll) to zoom
- Before / after view that renders exactly what the export will look like, adjustments and looks included
- Undo and redo for every change, dark and light themes, and drag-and-drop of files and folders
- **Arabic interface** (واجهة عربية) from version 3.0: every menu, label, tooltip, message and dialog is in Arabic and
  the window runs right to left. It follows Windows by default (Arabic if Windows is set to Arabic, otherwise
  English); choose **View → Language → English / العربية / Same as Windows** at any time, and restart the app to
  switch. The timeline and the video player stay left to right, because time runs that way. Numbers are written
  with the digits 0-9, and names like TikTok, MP4 and GIF stay in Latin letters. The translations are in
  [`video_converter/locales/ar.json`](video_converter/locales/ar.json); a new text only needs an entry there
- Each file keeps its own settings; **Apply to all files** copies them to the rest (each keeps its own crop,
  trim, cuts, texts and subtitle file; the logo and the subtitle style are copied). Presets never touch the texts
- Export queue that runs in the background with progress, time left, cancel and "show in folder"
- Tells you when a newer version is out: once a day the app asks GitHub for the latest release (a plain request
  that sends nothing but the app's name and version) and shows a small bar with **Download** and **Not now**.
  Turn it off under **Help → Check for updates**

**Video (MP4, H.265, WebM, MOV or GIF)**
- Choose the **Format** under Video quality: MP4 with H.264 (plays everywhere, the default), MP4 with H.265
  (smaller files, but older phones and programs can't play it), WebM (VP9 and Opus, for the web), MOV (H.264,
  for Apple's apps and video editors), or an animated GIF (looping, no sound, at most 15 frames a second and
  480 pixels wide by default; a note warns you when a GIF will run long enough to get big)
- **Fit in a file size** for Discord (10 MB), WhatsApp (16 MB), email (25 MB) or any size you type: the picture
  gets exactly what is left after the sound, in two passes, and a picture that can't look decent in that size
  is made smaller (720p, 480p, 360p) with a note in the queue, or the export says it can't be done. The file
  comes out at most that size, and usually within 3% of it
- Save the frame at the playhead as a PNG or JPG with **Save frame** (Ctrl+Shift+S), with all your edits applied
  and at the size the export has
- Reshape for phones (9:16), square posts (1:1), portrait posts (4:5) or widescreen (16:9)
- Crop to fill the frame, or fit the whole video with a blurred background or black bars
- Rotate, mirror, remove the sound, and pick the size (up to 1080p, 1080p, 720p, 480p), frame rate and quality
- Adjust brightness, contrast, saturation and gamma, sharpen, and reduce noise
- One-click color looks: black and white, warm, cool, vintage and high contrast, on top of the adjustments
- Stabilize shaky phone footage (light or strong). It runs when exporting, in two passes, so it takes longer
  and the preview doesn't show it
- Make videos play on your phone: a steady 60 or 30 fps frame rate (game recordings and screen captures often
  vary, which some phones refuse to play) with phone-safe H.264 and AAC, in one click with the
  **Play on phone** preset
- One-click presets for Play on phone, TikTok / Reels / Shorts, Instagram, YouTube and WhatsApp status, plus
  your own saved presets
- Phone recordings that are stored sideways are detected and shown the right way up
- Text on the video, as many pieces as you like: pick any installed font, size (as a share of the video's
  height, so it looks the same at any resolution), color, outline, a box behind it, bold, alignment, where it
  goes (drag it on the preview) and when it shows, with fade in and out. Arabic, Hebrew and other scripts
  that run right to left are joined and ordered correctly. Times are on the finished video, after cuts and speed
- A logo or watermark: any picture (a PNG with see-through parts works best), in a corner or dragged anywhere,
  with its own size and opacity, shown for the whole video
- Burned-in subtitles from an `.srt`, `.vtt` or `.ass` file made for the original video. They stay in sync
  after cuts, a trim and speed changes. Choose the font, size, color, outline and top, middle or bottom
- **Automatic captions**: *Make captions automatically…* under **Text and logo → Subtitles** listens to the
  speech and writes it down as captions, in Arabic, English, French, Spanish, German, Turkish, Hindi, Urdu and
  many other languages (or *Detect automatically*), optionally *translated to English*. It runs entirely on your
  PC: the first time you use an accuracy level its speech model is downloaded once from Hugging Face (Fast 75 MB,
  Balanced 141 MB, Accurate 464 MB), and after that no internet is needed. The captions are saved next to the video as `name.srt`,
  burned in, and can be fixed in **Edit captions…** (words, times, delete). See [Automatic captions](#automatic-captions)
- A thin track under the timeline shows when each text is on screen

**Audio (MP3)**
- 128 / 192 / 256 / 320 kbps or variable bitrate, keeping title and artist metadata

**Both**
- Trim to any range, e.g. from `0:15` to `1:02:30`, and cut out as many sections as you like
- Speed from 0.25× to 4× with natural-sounding audio (the pitch stays the same), and play backwards
- Fade in and out, with separate lengths for the picture and the sound
- Sound tools, per file: make it louder or quieter (−20 to +20 dB), make the loudness even (−14 LUFS, like
  YouTube and Spotify; it measures first and then applies, so it takes a little longer), and reduce background
  noise (light or strong)
- Background music from any audio file: set how loud it is next to the video's own sound, let it lower itself
  when people talk, and have it repeat if it is shorter than the video. It is laid over the finished result, so
  it follows your cuts, speed changes and joins, and fades out over the last 2 seconds. A video with no sound of
  its own just gets the music. Removing the sound removes the music too
- Never overwrites anything: edits are saved as `name (phone).mp4`, `name (square).mp4`, …,
  and existing names get a number (`file.mp3` becomes `file (1).mp3`)
- Accepts MP4, MOV, MKV, AVI, WEBM, M4V and other common video formats, and audio files (MP3, M4A, AAC, WAV,
  FLAC, OGG, OPUS) to trim, cut or save as MP3

## How to use

1. Click **Add files** or **Add folder**, or drop videos onto the window (or onto the exe)
2. Pick a file, then choose a preset or set the shape, crop, picture, trim, cuts, speed, fades, text and sound on the right
3. Click **Export** (or **Export all**); exports run in the queue at the bottom while you keep working

To make one video from several files, put them in the order you want (drag them in the list), select the
ones to join (or none, to join them all) and click **Join into one**. Each clip keeps its own crop, cuts,
speed, fades, volume and noise reduction; the result takes its size, frame rate, quality, even loudness and
background music from the first clip, and clips of another shape get black bars. If the first clip is set to
MP3, you get one MP3 instead.

### Videos that won't play on your phone

Game recordings and screen captures often have a frame rate that changes from moment to moment (and says it
could be 120 fps), and some phones just say the video is unsupported. To fix one:

1. Add the video and pick it in the list. If its frame rate is the problem, a note under **Frame rate** in
   **Video quality** says so.
2. Choose the **Play on phone (keeps the whole picture)** preset. It keeps the video's shape and sound, makes it
   no bigger than 1080p, and gives it a steady 60 fps. To use 30 fps instead, change **Frame rate** to
   **Steady 30 fps**.
3. Click **Export**. The result is saved next to the original as `name (phone-ready).mp4`.

To convert a whole folder, add the folder, choose the preset for one file, click **Apply to all files**, then
**Export all**. A video that is slower than the rate you choose keeps its own rate (a 30 fps video stays at 30 fps
with Steady 60), and the original is never changed.

### Videos that are too big to send

Discord, WhatsApp and email refuse big files. Under **Video quality**, set **Quality** to **Fit in a file size** and
choose **Discord (10 MB)**, **WhatsApp (16 MB)**, **Email (25 MB)** or **Custom…**. The note under it says what the
picture will get; a long video in a small size is made smaller (720p, 480p or 360p), and the queue tells you when
that happened. If even the smallest size would look terrible, the panel says how much of the video would fit, so
you can cut it shorter or pick a bigger size. Sizes count a megabyte as a million bytes, the way they are
advertised, so a "10 MB" file is under the limit however the other end counts it.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| Space | Play / pause |
| J / L | Back / forward 5 seconds |
| ← / → | Previous / next frame (hold Shift for 1 second) |
| I / O | Start / end the trim at the playhead |
| S | Split at the playhead |
| X | Cut out the part under the playhead |
| B | Before / after view |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| Ctrl+O | Add files |
| Ctrl+E / Ctrl+Shift+E | Export the selected file / export all |
| Ctrl+Shift+S | Save the frame at the playhead as a PNG or JPG |
| Ctrl+J | Join the files into one |

## Running from source

Requires Python 3.9+, the packages in `requirements.txt` (`pip install -r requirements.txt`), and ffmpeg
(`winget install Gyan.FFmpeg`, `brew install ffmpeg`, `sudo apt install ffmpeg`, or `pip install imageio-ffmpeg`). The app looks for ffmpeg in the `FFMPEG_PATH` environment variable, then on your `PATH`,
then in `imageio-ffmpeg`.

```bash
python -m video_converter
```

**Text, logo and subtitles.** Open **Text and logo** on the right and click **Add text**. Drag the text on the
preview to move it (the logo too); **Use playhead** sets when it appears and disappears, measured on the finished
video. In a joined export each clip has its own texts, timed within that clip. Subtitle files are timed for the
original video, so choose one only for the video it belongs to (a file in an old code page is read as Windows-1252).
Emoji need a font that has them and show as boxes in most fonts. The text goes over the picture last: a look
doesn't tint it and a fade to black doesn't hide it. The preview under **Before / after** is made with the same
filters as the export.

On Windows you can also double-click **`Free Video Editor.pyw`**.

### Automatic captions

Select a video with speech, open **Text and logo**, and under **Subtitles** click **Make captions
automatically…**. Choose the language (or leave it on *Detect automatically*) and how accurate to be, tick
**Translate to English** if you want the captions in English whatever is spoken, and click **Start**.

| Level | Download (once) | Good for |
| --- | --- | --- |
| Fast | 75 MB | Quick drafts and clear English speech |
| Balanced (default) | 141 MB | Most videos, in most languages |
| Accurate | 464 MB | Accents, noise, and languages like Arabic; the slowest |

- **Everything runs on your PC.** The speech models are not part of the program. The first time you pick a level,
  its model is downloaded once from Hugging Face (the official `Systran/faster-whisper-*` repositories, over
  HTTPS; no other site is contacted for this) into `%LOCALAPPDATA%\Free Video Editor\models`. Each file is checked
  against a SHA-256 hash that is built into the program, a download can be cancelled and picks up where it
  stopped, and a model that doesn't check out is never used. After that, captions work with no internet. Delete
  that folder to get the space back.
- **What is listened to**: the sound of the original file inside your trim (cuts are ignored, so if you put a cut
  part back its captions are there). The captions are timed for the original video, so they stay in sync through
  cuts, trims and speed changes just like any subtitle file.
- **The result** is saved as `name.srt` next to the video (`name (1).srt` if that exists) and added as the video's
  burned-in subtitles, in one undo step. Change the look (font, size, color, place) in the same section.
  **Edit captions…** opens a table where you can fix words, change times and delete captions; saving rewrites the
  `.srt` and is one undo step. Arabic and other right-to-left captions are shown correctly.
- Captions are cut into lines of at most 42 characters, two lines at most, between about 1 and 6 seconds,
  breaking at pauses and punctuation. Music and silence are skipped. It needs a video with a sound track; a
  video with no speech gives a message instead of captions. Very long videos take a while (the Balanced level is
  usually several times faster than real time on a modern PC); **Cancel** stops within a few seconds.
- Errors are explained in the window: no internet on the first use, a full disk, a video with no sound.

From the command line, `--auto-captions` writes the `.srt` and burns it into the exported video, and
`--captions-only` just writes the `.srt` (see the table below).

### Command line

Pass one or more files to convert them without opening the window:

```bash
python -m video_converter "My video.mp4" another.mp4 -q 320 -o converted
python -m video_converter "My video.mp4" --video phone --fit blur --start 0:10 --end 0:40
python -m video_converter "My video.mp4" --video original --cut 0:05-0:12 --cut 1:00-1:10 --speed 1.5
python -m video_converter intro.mp4 main.mp4 --video widescreen --fade-in 1 --fade-out 2 --join
python -m video_converter "Game clip.mp4" --video original --fps 60 --size up1080   # plays on a phone
python -m video_converter clip.mp4 --video original --text "My title" --text-pos 0.5,0.15 --text-end 4 --logo logo.png
python -m video_converter talk.mp4 --video original --subtitles talk.srt --subtitle-position bottom
python -m video_converter talk.mp4 --video original --auto-captions               # listen, write talk.srt, burn it in
python -m video_converter talk.mp4 --captions-only --caption-language ar --caption-model accurate   # just talk.srt
python -m video_converter "Big clip.mp4" --target-size 10                           # a copy that fits Discord's 10 MB
python -m video_converter clip.mp4 --format gif --start 0:05 --end 0:09             # a looping GIF
python -m video_converter clip.mp4 --video square --frame-at 0:12 --frame-out frame.png   # save one frame
```

| Option | Description |
| --- | --- |
| `-q`, `--quality` | MP3 quality: `128`, `192` (default), `256`, `320`, `vbr`, or `vbr-high` |
| `-o`, `--output-dir` | Folder for the results (default: next to each video) |
| `--start`, `--end` | Trim, e.g. `--start 0:15 --end 1:02:30` |
| `--overwrite` | Replace existing files instead of adding ` (1)` to the name |
| `--video` | Export an MP4 instead: `phone`, `square`, `portrait`, `widescreen`, or `original` |
| `--fit` | `crop` (default), `blur`, or `bars` |
| `--position X Y` | Crop position from 0 to 1 (default `0.5 0.5`, the center; other values are refused) |
| `--zoom` | Crop tighter, `1.0` to `3.0` (other values are refused) |
| `--rotate` | `90`, `180`, or `270` degrees clockwise |
| `--mirror`, `--mute` | Flip left-right; remove the sound |
| `--size` | Short side in pixels: `1080`, `720`, or `480`, or `up1080` for the source's size but at most 1080 (never enlarged). Default: the source's short side; phone, portrait and square stop at `1080` |
| `--fps` | `60` or `30` for a steady frame rate with phone-safe H.264 and AAC (a slower video keeps its own rate), or `source` (default) to leave it alone. `--video original --fps 60` makes a copy that plays on a phone, saved as `name (phone-ready).mp4` |
| `--video-quality` | `high`, `balanced` (default), or `small` |
| `--format` | `mp4` (H.264, default), `hevc` (H.265 in an MP4: smaller, but older phones can't play it; saved as `name (phone H.265).mp4`), `webm` (VP9 and Opus), `mov` (H.264), or `gif` (a looping picture with no sound). Without `--video` it makes a video of the original shape |
| `--target-size MB` | Make the file fit in this many megabytes (`10` Discord, `16` WhatsApp, `25` email), in two passes with the graphics card's bitrate control when it is used. The file comes out under that size, usually within 3% of it. A picture that can't look decent in that size is made smaller (and it says so), or the export fails with a message. Not for a GIF |
| `--gif-fps`, `--gif-width` | With `--format gif`: the most frames a second (default `15`) and the widest the GIF gets in pixels (default `480`). A GIF longer than 15 seconds prints a warning about its size |
| `--frame-at TIME`, `--frame-out FILE` | Save the frame at that time of the video as a `.png` or `.jpg`, with the picture options applied (`--video phone`, `--look bw`, and so on), instead of exporting |
| `--cut START-END` | Remove a section, e.g. `--cut 0:05-0:12`; repeat it to remove more |
| `--remove-silences` | Cut out the pauses (stretches with no sound), keeping a little silence around the speech. Works with MP3 and `--video` exports |
| `--silence-db`, `--silence-min`, `--silence-pad` | With `--remove-silences`: how quiet counts as silence, `-60` to `-20` dB (default `-35`); the shortest pause to cut, `0.3` to `3` seconds (default `0.6`); the silence left on each side of the speech, `0` to `0.5` seconds (default `0.15`) |
| `--speed` | `0.25` to `4`, e.g. `2` for twice as fast (default `1`) |
| `--reverse` | Play backwards (holds every frame in memory, so a long clip prints a warning) |
| `--fade-in`, `--fade-out` | Fade the picture and sound in from / out to black and silence, in seconds |
| `--join` | Make one file of all the inputs, in the order given (an MP4 with `--video`, else an MP3) |
| `--volume DB` | Louder or quieter, `-20` to `20` dB (default `0`) |
| `--normalize` | Make the loudness even at −14 LUFS, like YouTube and Spotify (measures first, then applies) |
| `--denoise-audio` | Reduce background noise: `light` or `strong` |
| `--music FILE` | Lay this music under the whole result: it repeats if it is shorter and fades out over the last 2 seconds. Works with `--join`, cuts and `--speed`; with `--mute` there is no sound at all |
| `--music-volume DB` | The music's volume, `-40` to `0` dB (default `-18`); needs `--music` |
| `--duck` | Lower the music while the video's own sound plays; needs `--music` |
| `--text TEXT` | Write this text on the video (`\n` starts a new line); needs `--video`. One text from the command line |
| `--text-start`, `--text-end` | When the text shows, on the finished video (after cuts and speed); default: the whole video |
| `--text-pos X,Y` | Where the middle of the text sits, as shares of the frame, e.g. `0.5,0.85` (default, bottom and centered) |
| `--text-size N`, `--text-color COLOR`, `--text-font FAMILY` | The text's height as a share of the video's height, `0.02` to `0.4` (default `0.06`); `#RRGGBB` or a name like `yellow` (default white); an installed font (default Arial) |
| `--logo FILE` | Show this picture over the whole video; needs `--video` |
| `--logo-pos`, `--logo-size N`, `--logo-opacity N` | `top-left`, `top-right`, `bottom-left`, `bottom-right` (default), `center`, or `X,Y` for the logo's middle; its width as a share of the video's, `0.03` to `0.8` (default `0.15`); `0.05` to `1` (default `1`) |
| `--subtitles FILE` | Burn in an `.srt`, `.vtt` or `.ass` file made for the original video. Its times move with cuts, the trim and `--speed`; not with `--join` |
| `--subtitle-size N`, `--subtitle-position`, `--subtitle-color` | Height as a share of the video's (default `0.05`); `bottom` (default), `middle` or `top`; `#RRGGBB` or a name |
| `--auto-captions` | Listen to the speech, save the captions as `name.srt` (next to the video, or in `-o`) and burn them into the exported video; needs `--video` (or `--format` / `--target-size`). Works with cuts, the trim and `--speed`; the `--subtitle-*` options set the look. Not with `--subtitles`, `--join` or `--frame-at`. The first use of a `--caption-model` downloads its model (once) |
| `--captions-only` | Only write the `.srt` (implies `--auto-captions`); `--start` and `--end` choose the part to listen to. Exit code 1 if no speech was found |
| `--caption-language CODE` | The language spoken: `ar`, `en`, `fr`, `es`, `de`, `tr`, `hi`, `ur`, … or `auto` (default) to detect it |
| `--caption-model` | `fast` (75 MB download), `balanced` (141 MB, default) or `accurate` (464 MB) |
| `--translate-captions` | Write the captions in English, whatever language is spoken |
| `--brightness`, `--contrast`, `--saturation`, `--gamma` | Adjust the picture, e.g. `--contrast 1.2` (`--help` lists the ranges) |
| `--sharpen`, `--denoise` | Sharpen (`0` to `1.5`) or reduce noise (`0` to `10`) |
| `--look` | `bw`, `warm`, `cool`, `vintage`, or `contrast` |
| `--stabilize` | `light` or `strong` |

## Building the exe

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements-build.lock
.venv\Scripts\python build_exe.py            # the portable exe: dist\Free Video Editor.exe
.venv\Scripts\python build_exe.py --onedir   # a folder: dist\Free Video Editor\
```

`requirements-build.lock` has the exact versions the releases are built with; `requirements-build.txt` lists the
loose ones (newest versions). Both builds include ffmpeg, the Qt libraries and the speech engine for automatic
captions (faster-whisper with CTranslate2; the speech models themselves are downloaded by the app when first
needed, not bundled). The single-file exe unpacks itself to a temporary folder every time it starts, which takes a
few seconds; the folder build does not, so the installer packs the folder. Expected sizes (3.0.0): the portable exe
about 130 MB, the folder about 345 MB, the installer about 95 MB. To check that a build can load the
speech engine without opening a window, run `build_exe.py --verify "dist\Free Video Editor.exe"` (or the folder), or
by hand `"dist\Free Video Editor.exe" --self-test-captions result.txt`: the exit code is 0 and `result.txt`
starts with `ok`.

To also build the installer, install [Inno Setup 6](https://jrsoftware.org/isdl.php) and run
`.venv\Scripts\python build_exe.py --onedir --installer`, which writes `dist\FreeVideoEditor-Setup-<version>.exe`
(`ISCC.exe` is found on `PATH`, in its usual install folder, or through the `ISCC` environment variable).
The installer script is `installer\FreeVideoEditor.iss`. The build refuses to run when
`licenses\FFMPEG-NOTICE.txt` no longer describes the ffmpeg it would bundle.

## Running the tests

```bash
python -m unittest discover -s tests -v
```

The tests generate short sample videos with ffmpeg and skip what they can't run: the conversion tests need
ffmpeg, and the editor tests (`tests/test_ui.py`) need PySide6, so run them from the build environment:
`.venv\Scripts\python -m unittest discover -s tests`. No test uses the network or writes to your models folder;
one optional test runs the real Fast speech model on made-up sound, and only if that model has already been
downloaded on the PC.
GitHub runs the whole suite on every push and pull request (`.github/workflows/tests.yml`).

## Releases

Bump `__version__` in `video_converter/__init__.py`, then push a tag with the same number, e.g. `v2.3.2`.
The **Release** workflow (`.github/workflows/release.yml`) refuses a tag that doesn't match `__version__`, runs
the tests, builds the portable exe and the installer, checks both (the speech-engine self-test, the file sizes, and a
silent install, start and uninstall of the installer), and creates a **draft** release with both files and their
SHA-256 checksums. Nothing is published until you publish the draft. If a draft for the tag already exists, the
files are uploaded to it; the workflow fails rather than replace the files of a release that is already published.
Running the workflow by hand from the Actions tab only builds the files and attaches them to the run. The actions it
uses are pinned to commit SHAs, and Dependabot proposes updates.

## License

Free Video Editor is released under the [MIT License](LICENSE).
The Windows builds bundle FFmpeg (GPLv3, as a separate program), Qt for Python (LGPLv3) and the speech engine
for automatic captions (faster-whisper, CTranslate2, ONNX Runtime, tokenizers, NumPy; MIT, BSD and Apache 2.0). The
Whisper models (MIT) are downloaded on request and not bundled. The license texts, the exact versions, links to the
source code (including a written offer for the FFmpeg source) and the notes on replacing the Qt libraries are in
[licenses/](licenses/): [FFMPEG-NOTICE.txt](licenses/FFMPEG-NOTICE.txt), [QT-NOTICE.txt](licenses/QT-NOTICE.txt)
and [SPEECH-NOTICE.txt](licenses/SPEECH-NOTICE.txt). The installer shows them and adds a **Licenses** shortcut.

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md).
