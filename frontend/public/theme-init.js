// Applied before first paint so dark-mode users never see a light flash.
// Also points the browser-chrome theme colour at the chosen theme: the
// media-query metas in index.html only follow the operating-system setting.
(function () {
  var dark = false;
  try {
    var stored = window.localStorage.getItem('ci-theme');
    dark = stored === 'dark' || (stored !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  } catch (error) {
    // Storage can be unavailable; the app applies the theme after load.
    return;
  }
  if (dark) document.documentElement.classList.add('dark');
  var metas = document.querySelectorAll('meta[name="theme-color"]');
  for (var i = 0; i < metas.length; i += 1) metas[i].setAttribute('content', dark ? '#0e0d0b' : '#faf7f2');
})();
