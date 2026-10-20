// Keeps the Download buttons pointing at the newest release.
// Without JavaScript (or if GitHub can't be reached) the installer button opens the latest-release page,
// and the portable button uses GitHub's permanent "latest" link, so nothing here can leave a dead link.
(function () {
  "use strict";

  var API = "https://api.github.com/repos/Ahmed41022/free-video-editor/releases/latest";

  var AR = (document.documentElement.lang || "").toLowerCase().indexOf("ar") === 0;

  function mb(bytes) {
    return (bytes / (1024 * 1024)).toFixed(0) + (AR ? " ميغابايت" : " MB");
  }

  function setNote(id, text) {
    var els = document.querySelectorAll('[data-note="' + id + '"]');
    for (var i = 0; i < els.length; i++) els[i].textContent = text;
  }

  function apply(release) {
    var assets = release.assets || [];
    var installer = null;
    var portable = null;
    for (var i = 0; i < assets.length; i++) {
      var name = assets[i].name || "";
      if (/^FreeVideoEditor-Setup-.*\.exe$/i.test(name)) installer = assets[i];
      else if (/^FreeVideoEditor\.exe$/i.test(name)) portable = assets[i];
    }
    var version = String(release.tag_name || "").replace(/^v/i, "");

    if (installer && installer.browser_download_url) {
      var links = document.querySelectorAll("[data-download='installer']");
      for (var a = 0; a < links.length; a++) links[a].href = installer.browser_download_url;
      setNote("installer", AR ? "المثبّت، الإصدار " + version + "، " + mb(installer.size)
        : "Installer, version " + version + ", " + mb(installer.size));
    }
    if (portable) {
      setNote("portable", AR ? "نسخة محمولة بلا تثبيت، " + mb(portable.size)
        : "Portable, no install, " + mb(portable.size));
    }
    var versions = document.querySelectorAll("[data-version]");
    for (var v = 0; v < versions.length; v++) versions[v].textContent = version;
  }

  if (!window.fetch) return;
  fetch(API, { headers: { Accept: "application/vnd.github+json" } })
    .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
    .then(apply)
    .catch(function () { /* keep the fallback links */ });
})();
