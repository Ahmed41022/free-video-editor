# Code signing for Free Video Editor

This page explains why the app should be signed, which options exist, what the owner has to do himself, and how
the release workflow uses the result. Everything on the repository side is already prepared: signing is switched
off and turns on with repository settings only (see [Switching it on](#switching-it-on)).

Costs and eligibility below were checked in October 2026 and change over time; confirm them on the linked pages
before applying.

## Why sign

The installer and the portable exe are unsigned, so Windows SmartScreen shows "Windows protected your PC" the
first time someone runs them. People who don't know the project reasonably stop there. A valid signature:

- shows a verified publisher name instead of "Unknown publisher";
- lets SmartScreen build a reputation for that publisher. A certificate does not remove the warning on day one,
  but the warning fades as more people run signed releases;
- proves the file was not changed after it was built.

Signing is not needed for the winget listing, and it does not change how the app works.

## Options

| | SignPath Foundation | Azure Artifact Signing | Traditional OV certificate |
|---|---|---|---|
| Cost | Free for open source | About 10 USD per month (Basic plan, 5,000 signatures) | Roughly 200 to 400 USD per year |
| Publisher name shown | "SignPath Foundation" | The verified name of the owner or organization | The name on the certificate |
| Works from GitHub Actions | Yes, official action | Yes, official action | Only with a cloud or hardware-token service |
| Main requirement | Accepted open source project, approval of every release | Identity validation; individuals only in the USA and Canada, organizations in the USA, Canada, EU and UK | Identity validation by the certificate authority |
| Fits this project | Best fit | Not available to individuals outside the USA and Canada | Possible, but costly and awkward in CI |

Notes on each:

**SignPath Foundation** ([signpath.org](https://signpath.org/), [terms](https://signpath.org/terms.html)).
Free code signing for open source. The certificate belongs to SignPath Foundation, so users see that name as the
publisher. Conditions from their terms, and where the project stands:

- OSI-approved license for all components, no proprietary code: MIT for the app; FFmpeg (GPLv3) and Qt for Python
  (LGPLv3) are bundled as upstream binaries, which the terms allow (they should be mentioned in the application).
- Actively maintained, already released in the form to be signed, functionality described on the download page:
  yes (the README and the website).
- Only binaries built from the project's own source, built by an automated build: yes, the Release workflow builds
  on GitHub Actions.
- Every team member uses multi-factor authentication on GitHub and SignPath: turn on 2FA for the GitHub account.
- Roles are defined (author, reviewer, approver) and the signing of each release is approved manually.
- A "Code signing policy" section appears on the home page and the download pages (a template is below).
- File metadata is fixed: product name "Free Video Editor", and the version in every build must match. The
  version resource written by `build_exe.py` already does both.
- Software must not collect data without a privacy policy, must uninstall cleanly (the Inno Setup installer does),
  and must not change system settings without telling the user.
- Acceptance is at SignPath Foundation's discretion.

**Azure Artifact Signing** (formerly Trusted Signing;
[quickstart](https://learn.microsoft.com/azure/artifact-signing/quickstart)). A Microsoft-run service with a monthly
fee. Individual validation uses the identity on the Azure billing account, and the service has been limited to
individuals in the USA and Canada and organizations in the USA, Canada, the EU and the UK. If that has changed and
the owner qualifies, it is the fastest route to a signature in his own name.

**OV certificate** from a certificate authority. Since 2023 the private key has to live on a hardware token or in a
cloud HSM, so a plain `.pfx` file in GitHub secrets no longer works; you need the vendor's cloud-signing product.
Extended validation (EV) certificates no longer give instant SmartScreen trust either, so they are not worth the
extra cost here.

## What the owner does for SignPath, step by step

Do these himself; they involve his identity and accounts.

1. **Prepare the repository** (about 15 minutes):
   - Turn on two-factor authentication for the GitHub account (Settings, Password and authentication).
   - Add the "Code signing policy" text below to the home page of the website and to the README (and to the release
     notes of signed releases). This can be done when applying, and again after approval with the exact wording
     SignPath gives.
2. **Apply** at [signpath.org/apply](https://signpath.org/apply). Have this information ready:
   - project name: Free Video Editor;
   - repository: https://github.com/Ahmed41022/free-video-editor (MIT license, Windows desktop app, Python/PySide6,
     built with PyInstaller and Inno Setup, FFmpeg and Qt bundled as upstream binaries);
   - download page: https://github.com/Ahmed41022/free-video-editor/releases and the website
     https://ahmed41022.github.io/free-video-editor/;
   - the files to sign: `FreeVideoEditor.exe` (portable) and `FreeVideoEditor-Setup-<version>.exe` (installer), and
     the exe inside the installer (the installer packs a folder; only its main exe is signed, not the Qt and
     Python libraries in it);
   - build system: GitHub Actions, workflow `.github/workflows/release.yml`;
   - the maintainer's name, email and GitHub account (he is the only author, reviewer and approver);
   - release history: the releases up to 2.7.0 are on the releases page.
3. **After approval**, in the SignPath web app:
   - create an organization (if one was not created for you) and note the **organization ID**;
   - add the predefined **GitHub.com trusted build system** to the organization and link it to the project;
   - create the **project** with slug `free-video-editor` and an **artifact configuration** with this content:

     ```xml
     <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
       <zip-file>
         <pe-file path="*.exe">
           <authenticode-sign/>
         </pe-file>
       </zip-file>
     </artifact-configuration>
     ```

     (SignPath may ask you to set product name and version restrictions on the project; use `Free Video Editor`.)
   - create a **signing policy** with slug `release-signing` that requires manual approval, and make yourself the
     approver;
   - create a user for CI with permission to submit requests to that policy, and create an **API token** for it.
4. **Configure GitHub** (Settings, Secrets and variables, Actions, or with the `gh` CLI):

   ```bash
   gh secret set SIGNPATH_API_TOKEN --repo Ahmed41022/free-video-editor
   gh variable set SIGNPATH_ORGANIZATION_ID --body "<organization id>" --repo Ahmed41022/free-video-editor
   gh variable set SIGNPATH_PROJECT_SLUG --body "free-video-editor" --repo Ahmed41022/free-video-editor
   gh variable set SIGNPATH_SIGNING_POLICY_SLUG --body "release-signing" --repo Ahmed41022/free-video-editor
   gh variable set SIGNING_ENABLED --body "true" --repo Ahmed41022/free-video-editor
   ```

5. **Try it**: run the Release workflow by hand from the Actions tab (it never creates a release when started by
   hand). SignPath emails you to approve each of the two requests (the exes, then the installer). When both are approved the
   workflow checks the signatures and uploads the signed files as the run's artifact.
6. After the first signed release, update the SmartScreen wording on the website and in the README: the warning may
   still appear for a while until the publisher builds a reputation.

### Code signing policy text (for the home page and README)

> **Code signing policy.** Free code signing provided by [SignPath.io](https://signpath.org/), certificate by
> [SignPath Foundation](https://signpath.org/). Roles: author, reviewer and approver: Ahmed Ragab
> ([Ahmed41022](https://github.com/Ahmed41022)). This program will not transfer any information to other networked
> systems unless specifically requested by the user. The only network request the website makes is to GitHub, to look
> up the newest version for the download buttons.

## How the release workflow uses it

`.github/workflows/release.yml` has an optional signing path in the `build` job. It runs only when the repository
variable `SIGNING_ENABLED` is `true`; otherwise the workflow builds exactly as before (unsigned) and says so in the
log.

When it is on:

1. `python build_exe.py` builds the portable exe and `python build_exe.py --onedir` builds the program folder that
   the installer packs (both are checked with the speech-engine self-test).
2. Both exes (the portable one and `Free Video Editor.exe` inside the folder) are copied to `sign-in/` as
   `portable.exe` and `app.exe`, uploaded as one workflow artifact and submitted to SignPath
   (`signpath/github-action-submit-signing-request`) in a single request, which waits for your approval and
   returns the signed files.
3. The signed exes replace the unsigned ones and `python build_exe.py --installer-only` builds the installer around
   the folder, so the exe inside the installer is signed too.
4. The installer is signed the same way (a second request).
5. The workflow checks the three files with `Get-AuthenticodeSignature` and fails if any is not validly signed.
6. The rest of the job (checksums, "exe starts" check, a silent install and uninstall of the installer, upload,
   draft release) runs on the signed files, so the portable exe, the installer and the SHA-256 list in the release
   notes all describe the signed files.

Because SignPath checks that each request comes from this repository's workflow, signing only works from the
GitHub-hosted run, not from a local build.

## If Azure Artifact Signing becomes an option

Replace the two SignPath steps with the `azure/artifact-signing-action` (formerly `azure/trusted-signing-action`),
which signs files in place on the runner, so no artifact upload is needed. The secrets are the Azure credentials of
the signing account (service principal with the "Artifact Signing Certificate Profile Signer" role) and the variables
are the account endpoint, account name and certificate profile name. The same `SIGNING_ENABLED` switch and the two
signing points (exe before the installer, then the installer) stay as they are.
