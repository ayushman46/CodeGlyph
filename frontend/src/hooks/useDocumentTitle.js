import { useEffect } from 'react';

export const APP_NAME = 'Codebase Intel';
export const DEFAULT_TITLE = `${APP_NAME} — cited answers and real pull requests for any GitHub repo`;

/** "Pricing" -> "Pricing · Codebase Intel"; empty -> the full default title. */
export const formatTitle = (title) => (title ? `${title} · ${APP_NAME}` : DEFAULT_TITLE);

/**
 * Set the tab title from a page. App sets a per-route default first (in a
 * layout effect), so a page calling this with a more specific title, e.g.
 * useDocumentTitle(`${repo} · Overview`), wins. Pass `undefined` to keep the
 * route default.
 */
const useDocumentTitle = (title) => {
  useEffect(() => {
    if (title === undefined) return;
    document.title = formatTitle(title);
  }, [title]);
};

export default useDocumentTitle;
