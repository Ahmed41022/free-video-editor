# winget manifests

Free Video Editor is published to the Windows Package Manager as `AhmedRagab.FreeVideoEditor`
(`winget install AhmedRagab.FreeVideoEditor`). The manifests in `manifests/` are a copy of what lives in
[microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs/tree/master/manifests/a/AhmedRagab/FreeVideoEditor);
the copy here is only a template for the next version. The real listing changes when a pull request there is merged.

## Publishing a new version

1. Publish the GitHub release first (the installer URL must work). The release must contain
   `FreeVideoEditor-Setup-<version>.exe`.
2. Create the new manifest folder:

   ```bash
   packaging/winget/new-version.sh 2.8.0
   winget validate --manifest packaging/winget/manifests/a/AhmedRagab/FreeVideoEditor/2.8.0
   ```

   The script copies the newest folder, changes the version everywhere, downloads the installer and writes its
   SHA-256 and the release date. Update `ReleaseNotesUrl`, and the description or tags if they changed, by hand.
3. Open a pull request to winget-pkgs titled `New version: AhmedRagab.FreeVideoEditor version 2.8.0`
   with only `manifests/a/AhmedRagab/FreeVideoEditor/2.8.0/`. Fork it, check out just that folder
   (`git clone --filter=blob:none --no-checkout --depth 1`, then `git sparse-checkout set manifests/a/AhmedRagab`),
   or use [wingetcreate](https://github.com/microsoft/winget-create):
   `wingetcreate update AhmedRagab.FreeVideoEditor --version 2.8.0 --urls <installer url> --submit`.
4. Wait for the automated checks on the PR. They install the package in a clean Windows machine; fix anything the
   bot asks for and push to the same branch.

## Notes

- The installer is an Inno Setup installer. `AppId` in `installer/FreeVideoEditor.iss` must never change: the
  manifest's `ProductCode` (`{6B1F6C0E-3A52-4C8E-9D43-7E5B2A9C1F84}_is1`) is derived from it, and winget uses it to
  recognise upgrades.
- Per-user is the default (`/CURRENTUSER`); `--scope machine` uses `/ALLUSERS` and asks for administrator rights.
- The portable `FreeVideoEditor.exe` is not listed, only the installer.
