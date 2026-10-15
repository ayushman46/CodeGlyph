import { useEffect, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigationType } from 'react-router-dom';
import { announce } from './useToasts';

/*
 * Route-change behaviour for a BrowserRouter app (no data router, so no
 * <ScrollRestoration>):
 * - a new page starts at the top (or at its #hash target);
 * - Back/Forward and reloads return to where the reader was, waiting for
 *   lazily loaded content to be tall enough, and giving up if they scroll;
 * - in-page changes (same path, new hash or query) are left to the page;
 * - focus moves to the new page's <h1> so keyboard and screen-reader users
 *   start at the content instead of the stale link they activated.
 */

const STORAGE_KEY = 'ci-scroll-positions';
const MAX_ENTRIES = 50;

const readPositions = () => {
  try {
    return JSON.parse(window.sessionStorage.getItem(STORAGE_KEY)) || {};
  } catch {
    return {};
  }
};

const positions = typeof window === 'undefined' ? {} : readPositions();

const savePosition = (key, y) => {
  if (!key) return;
  positions[key] = Math.round(y);
  const keys = Object.keys(positions);
  if (keys.length > MAX_ENTRIES) keys.slice(0, keys.length - MAX_ENTRIES).forEach((old) => delete positions[old]);
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(positions));
  } catch {
    // Storage can be full or disabled; restoration is best-effort.
  }
};

// Take over from the browser before anything renders, so it does not jump
// to an old offset while a lazily loaded page is still a short fallback.
if (typeof window !== 'undefined' && 'scrollRestoration' in window.history) {
  window.history.scrollRestoration = 'manual';
}

const scrollToY = (top) => window.scrollTo({ top, left: 0, behavior: 'instant' });

let cancelPendingRestore = () => {};

const restorePosition = (y) => {
  cancelPendingRestore();
  if (!y) {
    scrollToY(0);
    return;
  }
  let frame = 0;
  let handle = 0;
  const events = ['wheel', 'touchstart', 'keydown', 'pointerdown'];
  const cleanup = () => {
    window.cancelAnimationFrame(handle);
    events.forEach((name) => window.removeEventListener(name, cleanup));
  };
  cancelPendingRestore = cleanup;
  // The reader taking over (scrolling, typing, clicking) cancels the restore.
  events.forEach((name) => window.addEventListener(name, cleanup, { passive: true }));
  const attempt = () => {
    const max = document.documentElement.scrollHeight - window.innerHeight;
    // ~2s of frames for lazy pages and data to fill in.
    if (max >= y || frame > 120) {
      scrollToY(Math.min(y, Math.max(0, max)));
      cleanup();
      return;
    }
    frame += 1;
    handle = window.requestAnimationFrame(attempt);
  };
  attempt();
};

const scrollToHash = (hash) => {
  let id = hash.slice(1);
  try {
    id = decodeURIComponent(id);
  } catch {
    // Keep the raw id.
  }
  const target = id && document.getElementById(id);
  if (target) target.scrollIntoView({ block: 'start' });
  else scrollToY(0);
};

const navigationKind = () => {
  try {
    return window.performance.getEntriesByType('navigation')[0]?.type || 'navigate';
  } catch {
    return 'navigate';
  }
};

/**
 * Window scroll handling across route changes. `ignore(location)` returns
 * true for routes that manage their own scrolling (e.g. a full-height app
 * shell whose window never scrolls).
 */
export const useScrollRestoration = ({ ignore } = {}) => {
  const location = useLocation();
  const navigationType = useNavigationType();
  const current = useRef({ key: location.key, pathname: location.pathname });
  const lastY = useRef(0);
  const first = useRef(true);

  // Track the latest offset continuously: by the time a route change
  // commits, the old page is gone and the browser may have clamped scrollY.
  useEffect(() => {
    let frame = 0;
    const onScroll = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        lastY.current = window.scrollY;
      });
    };
    const onPageHide = () => savePosition(current.current.key, window.scrollY);
    lastY.current = window.scrollY;
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('pagehide', onPageHide);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('pagehide', onPageHide);
    };
  }, []);

  useLayoutEffect(() => {
    if (first.current) {
      first.current = false;
      const kind = navigationKind();
      if ((kind === 'reload' || kind === 'back_forward') && positions[location.key] && !ignore?.(location)) {
        restorePosition(positions[location.key]);
      } else if (location.hash) {
        // Let the page render its anchors first.
        window.requestAnimationFrame(() => scrollToHash(location.hash));
      }
      return;
    }
    const previous = current.current;
    if (previous.key === location.key) return;
    savePosition(previous.key, lastY.current);
    current.current = { key: location.key, pathname: location.pathname };
    if (ignore?.(location)) return;
    if (navigationType === 'POP') {
      restorePosition(positions[location.key] || 0);
      return;
    }
    // Same page, new hash or query: the page decides (tabs, doc sections).
    if (previous.pathname === location.pathname) return;
    cancelPendingRestore();
    if (location.hash) window.requestAnimationFrame(() => scrollToHash(location.hash));
    else scrollToY(0);
  }, [location]); // eslint-disable-line react-hooks/exhaustive-deps
};

const focusableHeading = () => document.querySelector('main h1') || document.querySelector('main');

/**
 * After a path change, move focus to the new page's main heading (once the
 * lazily loaded page has rendered it), or announce the page title instead
 * when `shouldFocus(location)` is false (e.g. inside the workspace, which
 * manages its own focus). Never steals focus the user has already moved,
 * and never reaches behind an open dialog.
 */
export const useRouteFocus = ({ shouldFocus = () => true, shouldAnnounce = () => true } = {}) => {
  const location = useLocation();
  const previousPath = useRef(location.pathname);

  useEffect(() => {
    if (previousPath.current === location.pathname) return undefined;
    const fromPath = previousPath.current;
    previousPath.current = location.pathname;

    if (!shouldFocus(location, fromPath)) {
      if (shouldAnnounce(location, fromPath)) {
        // Give the page a moment to set a more specific title.
        const timer = window.setTimeout(() => announce(document.title), 120);
        return () => window.clearTimeout(timer);
      }
      return undefined;
    }

    const startedOn = document.activeElement;
    let frame = 0;
    let tries = 0;
    const userMovedFocus = () => {
      const active = document.activeElement;
      if (!active || active === document.body || active === startedOn) return false;
      // Focus still in the persistent header (the nav link that was used).
      return !active.closest?.('header');
    };
    const attempt = () => {
      if (userMovedFocus() || document.querySelector('[aria-modal="true"]')) return;
      const target = focusableHeading();
      // Wait for the real page (not a loading fallback) to provide its h1.
      if ((!target || target.tagName !== 'H1') && tries < 180) {
        tries += 1;
        frame = window.requestAnimationFrame(attempt);
        return;
      }
      if (!target) return;
      if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
      target.focus({ preventScroll: true });
    };
    frame = window.requestAnimationFrame(attempt);
    return () => window.cancelAnimationFrame(frame);
  }, [location]); // eslint-disable-line react-hooks/exhaustive-deps
};
