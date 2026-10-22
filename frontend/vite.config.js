import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { brotliCompressSync, constants, gzipSync } from 'node:zlib'

// Opening /app directly (a bookmark, a reload, the sign-in redirect) used to
// fetch the workspace chunk only after the entry chunk had downloaded and
// run. This emits a tiny same-origin script (the CSP allows no inline JS)
// that, on /app paths, preloads the workspace chunk and its imports alongside
// the entry. Elsewhere they are prefetched at low priority (never on Save-Data
// or 2G): once idle for a returning user with a saved session, else when the
// visitor points at or focuses a way in (a link to /app, a sign-in button),
// so a first-time visitor who never signs in downloads nothing extra.
const WORKSPACE_PAGE = '/src/pages/DashboardPage.jsx'
const workspacePreload = () => {
  let base = '/'
  return {
    name: 'workspace-preload',
    apply: 'build',
    configResolved(config) {
      base = config.base
    },
    generateBundle(_options, bundle) {
      const page = Object.values(bundle).find((item) => item.type === 'chunk' && item.facadeModuleId?.endsWith(WORKSPACE_PAGE))
      if (!page) return
      const files = [page.fileName, ...page.imports].map((file) => `${base}${file}`)
      // The code font: found only once code is on screen, it arrived after the
      // first render and re-wrapped every citation and code block (on a phone
      // the conversation grew a sixth taller and slid down as it settled).
      const codeFont = Object.values(bundle).find((item) => item.type === 'asset' && /jetbrains-mono-latin-wght-normal-[^/]*\.woff2$/.test(item.fileName))
      this.emitFile({
        type: 'asset',
        fileName: 'workspace-preload.js',
        source: `(function () {
  var files = ${JSON.stringify(files)};
  var add = function (rel) {
    files.forEach(function (href) {
      var link = document.createElement('link');
      link.rel = rel;
      link.href = href;
      if (rel === 'prefetch') link.as = 'script';
      document.head.appendChild(link);
    });
  };
  if (/^\\/app(\\/|$)/.test(window.location.pathname)) {
    var font = ${JSON.stringify(codeFont ? `${base}${codeFont.fileName}` : '')};
    if (font) {
      var preload = document.createElement('link');
      preload.rel = 'preload';
      preload.as = 'font';
      preload.type = 'font/woff2';
      preload.crossOrigin = 'anonymous';
      preload.href = font;
      document.head.appendChild(preload);
    }
    return add('modulepreload');
  }
  var connection = navigator.connection;
  if (connection && (connection.saveData || /2g/.test(connection.effectiveType || ''))) return;
  var done = false;
  var prefetch = function () {
    if (done) return;
    done = true;
    add('prefetch');
  };
  var signedIn = false;
  try {
    for (var i = 0; i < window.localStorage.length; i += 1) {
      if (/^sb-.+-auth-token$/.test(window.localStorage.key(i) || '')) signedIn = true;
    }
  } catch (error) {
    // Storage can be unavailable; intent below still prefetches.
  }
  if (signedIn) {
    window.addEventListener('load', function () {
      var idle = window.requestIdleCallback || function (callback) { return setTimeout(callback, 2000); };
      idle(prefetch, { timeout: 5000 });
    });
  }
  var intent = function (event) {
    var target = event.target;
    if (target && target.closest && target.closest('a[href^="/app"], [data-prefetch-app]')) prefetch();
  };
  ['pointerover', 'focusin', 'touchstart'].forEach(function (type) {
    document.addEventListener(type, intent, { capture: true, passive: true });
  });
})();
`,
      })
    },
    transformIndexHtml: {
      order: 'post',
      // async, first in <head>: it runs as soon as it arrives, not after the
      // entry module (deferred scripts run in document order).
      handler: () => [{ tag: 'script', attrs: { src: `${base}workspace-preload.js`, async: true }, injectTo: 'head-prepend' }],
    },
  }
}

// Brotli and gzip copies of the text assets, written once at build time at
// maximum quality. The single-service deploy serves them as they are
// (backend/frontend_static.py) instead of compressing every request on a
// small instance, and brotli 11 is about a fifth smaller than the on-the-fly
// encoding the CDN used to apply. Hosts that compress themselves ignore them.
const COMPRESSIBLE = /\.(js|css|svg|json|txt)$/
const precompressAssets = () => {
  let assets = ''
  return {
    name: 'precompress-assets',
    apply: 'build',
    configResolved(config) {
      assets = join(resolve(config.root, config.build.outDir), config.build.assetsDir)
    },
    closeBundle() {
      if (!existsSync(assets)) return
      for (const name of readdirSync(assets)) {
        if (!COMPRESSIBLE.test(name)) continue
        const file = join(assets, name)
        const source = readFileSync(file)
        if (source.length < 1024) continue
        const brotli = brotliCompressSync(source, {
          params: { [constants.BROTLI_PARAM_QUALITY]: 11, [constants.BROTLI_PARAM_SIZE_HINT]: source.length },
        })
        if (brotli.length < source.length) writeFileSync(`${file}.br`, brotli)
        const gzip = gzipSync(source, { level: 9 })
        if (gzip.length < source.length) writeFileSync(`${file}.gz`, gzip)
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), workspacePreload(), precompressAssets()],
  // The project keeps its shared local configuration at the repository root.
  // Without this, Vite only checks frontend/.env and Google sign-in falls back
  // to the disabled authentication client during local development.
  envDir: '..',
  server: {
    port: 5173
  }
})
