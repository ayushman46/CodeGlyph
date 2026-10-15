import { Monitor, Moon, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';
import { getThemePreference, setThemePreference, watchSystemTheme } from '../lib/theme';

const ORDER = ['light', 'dark', 'system'];
const ICONS = { light: Sun, dark: Moon, system: Monitor };
const LABELS = { light: 'Light theme', dark: 'Dark theme', system: 'System theme' };

/* Matches --color-canvas in each theme (and the theme-color metas in
 * index.html), so the mobile browser chrome follows the in-app choice rather
 * than only the operating-system setting. */
const CANVAS = { light: '#faf7f2', dark: '#0e0d0b' };

const syncThemeColor = () => {
  const color = document.documentElement.classList.contains('dark') ? CANVAS.dark : CANVAS.light;
  document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => meta.setAttribute('content', color));
};

const ThemeToggle = ({ className = '' }) => {
  const [preference, setPreference] = useState(getThemePreference);
  const [announcement, setAnnouncement] = useState('');

  useEffect(() => {
    syncThemeColor();
    return watchSystemTheme(() => {
      setPreference(getThemePreference());
      syncThemeColor();
    });
  }, []);

  const Icon = ICONS[preference];
  const next = ORDER[(ORDER.indexOf(preference) + 1) % ORDER.length];

  const cycle = () => {
    setThemePreference(next);
    setPreference(next);
    syncThemeColor();
    setAnnouncement(`${LABELS[next]} on`);
  };

  return (
    <>
      <button
        type="button"
        onClick={cycle}
        className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink md:h-9 md:w-9 ${className}`}
        aria-label={`${LABELS[preference]}. Switch to ${LABELS[next].toLowerCase()}`}
        title={`${LABELS[preference]} (click for ${LABELS[next].toLowerCase()})`}
      >
        <Icon className="h-4 w-4" aria-hidden="true" />
      </button>
      {/* The button's own name changes too, but not every screen reader
          re-reads the name of the focused control. */}
      <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
    </>
  );
};

export default ThemeToggle;
