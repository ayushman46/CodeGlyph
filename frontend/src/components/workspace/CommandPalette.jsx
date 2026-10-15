import {
  BookOpen, CreditCard, FolderGit2, GitFork, LayoutDashboard, MessageSquareText, Moon, Pencil, PieChart, Plus, RefreshCw,
  Search, Square, Trash2, User,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { parseRepositoryInput } from '../../lib/format';
import { statusSentence } from '../../lib/phase';
import { getThemePreference, resolveTheme, setThemePreference } from '../../lib/theme';
import useStore from '../../store/useStore';
import useFocusTrap from '../../hooks/useFocusTrap';
import Kbd from '../ui/Kbd';

const score = (text, query) => {
  const haystack = text.toLowerCase();
  const needle = query.toLowerCase().trim();
  if (!needle) return 1;
  if (haystack.includes(needle)) return 3 - haystack.indexOf(needle) / Math.max(1, haystack.length);
  let position = 0;
  for (const character of needle) {
    position = haystack.indexOf(character, position);
    if (position === -1) return 0;
    position += 1;
  }
  return 1;
};

const dotClass = (repo) => {
  if (repo.status === 'failed') return 'bg-danger';
  if (repo.status === 'cancelled') return 'bg-ink-3';
  if (repo.is_indexing) return 'bg-warning';
  return 'bg-success';
};

/**
 * ⌘K palette: jump to a repository, act on the open one, or start indexing
 * a typed owner/repo. Arrow keys move, Enter runs, Escape closes.
 */
const CommandPalette = ({ open, onClose, onAddRepository, currentRepo = null, onRepoAction, onShowTab }) => {
  const repos = useStore((state) => state.repos);
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef(null);
  const dialogRef = useRef(null);
  const listRef = useRef(null);
  useFocusTrap(dialogRef, open, { onEscape: () => onClose(), initialFocusRef: inputRef });

  useEffect(() => {
    if (open) {
      setQuery('');
      setActive(0);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const items = useMemo(() => {
    const go = (path) => () => navigate(path);
    const repoPath = (name) => `/app/r/${encodeURIComponent(name)}`;
    const typed = parseRepositoryInput(query);
    const known = typed.ok ? repos.find((repo) => (repo.full_name || '').toLowerCase() === typed.fullName.toLowerCase()) : null;
    const current = currentRepo;
    const act = (type) => () => onRepoAction?.(type, current);
    const all = [
      // A typed owner/repo offers to add it (the add form asks for confirmation).
      ...(typed.ok && query.includes('/') && !known ? [{
        id: 'index-typed', label: `Index ${typed.fullName}`, hint: 'Opens the add form, prefilled', icon: Plus, group: 'Add', pinned: true,
        run: () => navigate('/app', { state: { prefill: query.trim() } }),
      }] : []),
      ...repos.map((repo) => ({
        id: `repo-${repo.id}`, label: repo.repo_name, hint: repo.full_name, status: repo, current: current?.id === repo.id,
        icon: FolderGit2, group: 'Repositories', run: go(repoPath(repo.repo_name)),
      })),
      ...(current ? [
        { id: 'tab-chat', label: 'Open chat', icon: MessageSquareText, group: current.repo_name, run: () => onShowTab?.('chat') },
        { id: 'tab-overview', label: 'Open overview', icon: LayoutDashboard, group: current.repo_name, run: () => onShowTab?.('overview') },
        { id: 'coverage', label: 'Coverage report', icon: PieChart, group: current.repo_name, run: act('coverage') },
        ...(current.queryable ? [{ id: 'impact', label: 'Change impact', icon: GitFork, group: current.repo_name, run: act('impact') }] : []),
        current.is_indexing
          ? { id: 'stop', label: 'Stop indexing', icon: Square, group: current.repo_name, run: act('stop') }
          : { id: 'reindex', label: 'Re-index', icon: RefreshCw, group: current.repo_name, run: act('reindex') },
        { id: 'rename', label: 'Rename repository', icon: Pencil, group: current.repo_name, run: act('rename') },
        { id: 'delete', label: 'Delete repository', icon: Trash2, group: current.repo_name, danger: true, run: act('delete') },
      ] : []),
      { id: 'add', label: 'Add a repository', icon: Plus, group: 'Actions', run: onAddRepository },
      { id: 'theme', label: 'Toggle dark mode', icon: Moon, group: 'Actions', run: () => setThemePreference(resolveTheme(getThemePreference()) === 'dark' ? 'light' : 'dark') },
      { id: 'account', label: 'Account & usage', icon: User, group: 'Go to', run: go('/account') },
      { id: 'pricing', label: 'Pricing', icon: CreditCard, group: 'Go to', run: go('/pricing') },
      { id: 'docs', label: 'Documentation', icon: BookOpen, group: 'Go to', run: go('/docs') },
    ];
    if (!query.trim()) return all;
    return all
      .map((item) => ({
        ...item,
        score: item.pinned ? 4 : Math.max(score(item.label, query), item.hint ? score(item.hint, query) * 0.9 : 0, item.group !== 'Repositories' ? score(`${item.group} ${item.label}`, query) * 0.8 : 0),
      }))
      .filter((item) => item.score > 0)
      .sort((a, b) => b.score - a.score);
  }, [repos, query, navigate, onAddRepository, currentRepo, onRepoAction, onShowTab]);

  useEffect(() => setActive(0), [query]);

  // Keep the highlighted option visible while moving with the keyboard.
  useEffect(() => {
    listRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: 'nearest' });
  }, [active, items]);

  if (!open) return null;
  const run = (item) => {
    if (!item) return;
    onClose();
    item.run?.();
  };
  const grouped = !query.trim();

  return createPortal(
    <div className="fixed inset-0 z-[95] flex items-start justify-center bg-black/40 p-3 pt-[10vh] backdrop-blur-[2px] animate-fade-in sm:p-4 sm:pt-[12vh]" onMouseDown={onClose} role="presentation">
      <div className="flex max-h-[76dvh] w-full max-w-lg animate-rise flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-lift)]" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Command palette" tabIndex={-1} onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex items-center gap-2.5 border-b border-line px-4">
          <Search className="h-4 w-4 shrink-0 text-ink-3" aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowDown') { event.preventDefault(); setActive((value) => (items.length ? (value + 1) % items.length : 0)); }
              if (event.key === 'ArrowUp') { event.preventDefault(); setActive((value) => (items.length ? (value - 1 + items.length) % items.length : 0)); }
              if (event.key === 'Home' && !query) { event.preventDefault(); setActive(0); }
              if (event.key === 'End' && !query) { event.preventDefault(); setActive(Math.max(0, items.length - 1)); }
              if (event.key === 'Enter') { event.preventDefault(); run(items[active]); }
            }}
            placeholder="Jump to a repository, run an action, or type owner/repo…"
            className="h-12 min-w-0 flex-1 bg-transparent text-[14px] text-ink outline-none placeholder:text-ink-3"
            aria-label="Search commands"
            role="combobox"
            aria-expanded="true"
            aria-autocomplete="list"
            aria-controls="command-results"
            aria-activedescendant={items[active] ? `command-${items[active].id}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <Kbd>Esc</Kbd>
        </div>
        <ul id="command-results" ref={listRef} role="listbox" aria-label="Commands" className="scroll-thin min-h-0 flex-1 overflow-y-auto p-1.5">
          {items.length === 0 && (
            <li role="presentation" className="px-3 py-6 text-center text-[13px] text-ink-3">
              No matches for “{query.trim()}”. Type owner/repo to add a repository.
            </li>
          )}
          {items.map((item, index) => {
            const header = grouped && (index === 0 || items[index - 1].group !== item.group);
            return (
              <li key={item.id} role="presentation">
                {header && <p className="px-3 pb-1 pt-2.5 text-[10.5px] font-semibold uppercase tracking-wider text-ink-3" aria-hidden="true">{item.group}</p>}
                <div
                  id={`command-${item.id}`}
                  role="option"
                  aria-selected={index === active}
                  onMouseMove={() => { if (index !== active) setActive(index); }}
                  onClick={() => run(item)}
                  className={`flex w-full cursor-pointer items-center gap-3 rounded-xl px-3 py-2 text-left ${index === active ? 'bg-surface-2' : ''}`}
                >
                  {item.status ? (
                    <span className="flex h-4 w-4 shrink-0 items-center justify-center" aria-hidden="true"><span className={`h-2 w-2 rounded-full ${dotClass(item.status)}`} /></span>
                  ) : <item.icon className={`h-4 w-4 shrink-0 ${item.danger ? 'text-danger' : 'text-ink-3'}`} aria-hidden="true" />}
                  <span className={`min-w-0 flex-1 truncate text-[13.5px] ${item.danger ? 'text-danger' : 'text-ink'}`}>
                    {item.label}
                    {item.status && <span className="sr-only">, {statusSentence(item.status)}</span>}
                  </span>
                  {item.current && <span className="shrink-0 rounded-full bg-accent-soft px-1.5 py-px text-[10.5px] font-medium text-accent-strong">Open</span>}
                  {item.hint && <span className="hidden truncate font-mono text-[11px] text-ink-3 sm:inline">{item.hint}</span>}
                  {!grouped && <span className="shrink-0 text-[10.5px] text-ink-3">{item.group}</span>}
                </div>
              </li>
            );
          })}
        </ul>
        <div className="hidden items-center gap-3 border-t border-line px-4 py-2 text-[11px] text-ink-3 sm:flex" aria-hidden="true">
          <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd> move</span>
          <span className="flex items-center gap-1"><Kbd>↵</Kbd> open</span>
          <span className="flex items-center gap-1"><Kbd>Esc</Kbd> close</span>
        </div>
      </div>
    </div>,
    document.body,
  );
};

export default CommandPalette;
