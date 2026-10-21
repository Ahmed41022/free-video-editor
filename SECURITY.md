# Security policy

## Supported versions

Security fixes go into the latest release of Free Video Editor. Please update to the
[latest version](https://github.com/Ahmed41022/free-video-editor/releases/latest) before reporting, and say which
version you were using.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

1. Use GitHub's private reporting: open the
   [Security tab](https://github.com/Ahmed41022/free-video-editor/security) of the repository and choose
   **Report a vulnerability**. Only the maintainer can see it.
2. If that is not available to you, open a normal
   [issue](https://github.com/Ahmed41022/free-video-editor/issues) that says only that you have a security
   report (no details, no exploit), and the maintainer will arrange a private way to receive it.

A report is most useful with: the version of Free Video Editor, your Windows version, what you did, what you
expected and what happened, and a small file that triggers the problem if the problem depends on a file.

You can expect an answer within about a week. This is a volunteer project, so fixes take as long as they take,
but a confirmed vulnerability is handled before other work. When a fix is released, the report is credited in the
release notes unless you prefer not to be named.

## What counts

Examples of things to report: a media file, subtitle file, project file or speech model that makes the program run
code, write outside the folders it should use, or leak data; a flaw in the installer or uninstaller; a way to make
the update check or the model download contact or run something other than what is documented.

## What the program does on the network

Everything runs on your computer. The program connects to the internet only for an optional once-a-day check for a
newer version (GitHub) and, if you use automatic captions, a one-time download of the speech model you choose
(Hugging Face, over HTTPS, each file checked against a SHA-256 hash built into the program). "Report a problem"
opens your web browser; nothing is sent until you submit the page there.

## Downloads

Download the program only from the
[releases page](https://github.com/Ahmed41022/free-video-editor/releases). Each release lists the SHA-256 checksum
of its files, and the files are built from this repository by GitHub Actions.

## Third-party components

Free Video Editor bundles FFmpeg, Qt and a speech engine (see [licenses/](licenses/)). A vulnerability in one of
them is best reported to that project as well; tell the maintainer here too so the bundled version can be updated.
Dependabot watches the Python packages and the GitHub Actions used to build the program.
