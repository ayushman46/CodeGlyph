const STORAGE_KEY = 'ci-theme';
// Must match --color-canvas in index.css (also used by public/theme-init.js).
const CANVAS = { light: '#faf7f2', dark: '#0e0d0b' };

export const getThemePreference = () => {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {
    return 'system';
  }
};

export const resolveTheme = (preference) => {
  if (preference === 'light' || preference === 'dark') return preference;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
};

/** Browser chrome (mobile address bar, PWA title bar) follows the app theme,
    not only the OS theme the static <meta media> tags describe. */
const syncThemeColor = (theme) => {
  document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
    meta.setAttribute('content', CANVAS[theme]);
  });
};

/**
 * Swap themes in one frame: without this every surface with a colour
 * transition fades at its own speed and the page visibly "washes" across.
 */
const withoutTransitions = (apply) => {
  const root = document.documentElement;
  root.classList.add('theme-switching');
  apply();
  // Force a style flush with transitions off, then re-enable them.
  void window.getComputedStyle(root).color;
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => root.classList.remove('theme-switching'));
  });
};

export const applyTheme = (preference = getThemePreference()) => {
  const theme = resolveTheme(preference);
  const root = document.documentElement;
  const changed = root.classList.contains('dark') !== (theme === 'dark');
  const apply = () => {
    root.classList.toggle('dark', theme === 'dark');
    syncThemeColor(theme);
  };
  if (changed) withoutTransitions(apply);
  else apply();
  return theme;
};

export const setThemePreference = (preference) => {
  try {
    if (preference === 'system') window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, preference);
  } catch {
    // Storage may be unavailable (private mode); the theme still applies.
  }
  return applyTheme(preference);
};

/**
 * Follow OS theme changes while the preference is "system", and keep every
 * open tab in step when the preference is changed in another one.
 */
export const watchSystemTheme = (callback) => {
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  const onSystem = () => {
    if (getThemePreference() === 'system') callback(applyTheme('system'));
  };
  const onStorage = (event) => {
    if (event.key !== null && event.key !== STORAGE_KEY) return;
    callback(applyTheme(getThemePreference()));
  };
  media?.addEventListener?.('change', onSystem);
  window.addEventListener('storage', onStorage);
  return () => {
    media?.removeEventListener?.('change', onSystem);
    window.removeEventListener('storage', onStorage);
  };
};
