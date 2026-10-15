import {
  AlertTriangle, CheckCircle2, Download, ExternalLink, GitBranch, GitCommitHorizontal, GitFork, Info, LayoutDashboard,
  MessageSquareText, MoreHorizontal, Pencil, PieChart, RefreshCw, Square, SquarePen, Tag, Trash2, X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import useFocusTrap from '../../hooks/useFocusTrap';
import useMediaQuery from '../../hooks/useMediaQuery';
import { errorMessage } from '../../api/client';
import { parseRepository } from '../../lib/citations';
import { formatCount, shortSha } from '../../lib/format';
import { STEPS, isKeywordOnly, overallProgress, phaseIndex, phaseLabel, remainingLabel, statusTone } from '../../lib/phase';
import { downloadText, slugify } from '../../lib/patch';
import useStore from '../../store/useStore';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Menu from '../ui/Menu';
import ProgressBar from '../ui/ProgressBar';
import Spinner from '../ui/Spinner';
import Composer from './Composer';
import IngestionProgress, { useIndexingEta } from './IngestionProgress';
import MessageBubble from './MessageBubble';
import RepoOverview from './RepoOverview';
import SourcesPanel from './SourcesPanel';

const TABS = [['chat', 'Chat', MessageSquareText], ['overview', 'Overview', LayoutDashboard]];

const exportMarkdown = (repo, messages) => {
  const lines = [`# ${repo.full_name || repo.repo_name}`, '', `Exported from Codebase Intel on ${new Date().toLocaleString()}.`, ''];
  messages.forEach((message) => {
    lines.push(message.role === 'user' ? `## Q: ${message.content}` : message.content, '');
    if (message.role === 'assistant' && message.citations?.length) {
      lines.push('Sources:');
      message.citations.forEach((citation) => lines.push(`- ${citation.file_path}:L${citation.start_line}-L${citation.end_line}${citation.github_url ? ` (${citation.github_url})` : ''}`));
      lines.push('');
    }
  });
  return lines.join('\n');
};

/** Likely cause and fix for a failed index, from the worker's message. */
const failureAdvice = (message = '') => {
  if (/not found|404|private|not visible/i.test(message)) return 'Check that the repository exists and is public. Private repositories are not supported.';
  if (/quota|indexed-source|is full|limit/i.test(message)) return 'Delete a repository you no longer need to free space, or see the plans for a larger limit.';
  if (/no supported|no readable/i.test(message)) return 'The repository has no text source files that can be indexed (only binaries or unsupported formats).';
  if (/rate limit|timed out|timeout|unavailable|try again/i.test(message)) return 'This is usually temporary. Wait a moment and try again.';
  return 'This is often temporary. Try again; if it keeps failing, check the repository on GitHub.';
};

/** Dismissed notices survive tab switches and reloads for this session. */
const NOTICE_KEY = 'ci-dismissed-notices';
const readDismissed = () => {
  try {
    return new Set(JSON.parse(window.sessionStorage.getItem(NOTICE_KEY) || '[]'));
  } catch {
    return new Set();
  }
};
const rememberDismissed = (key) => {
  try {
    const all = readDismissed();
    all.add(key);
    window.sessionStorage.setItem(NOTICE_KEY, JSON.stringify([...all].slice(-50)));
  } catch {
    // Storage may be unavailable; the notice simply returns next time.
  }
};

const Notice = ({ tone = 'info', icon: Icon = Info, children, action, onDismiss, live = false }) => {
  const tones = {
    info: 'border-line bg-surface-2/70 text-ink-2',
    success: 'border-success/20 bg-success-soft text-ink',
    warning: 'border-warning/25 bg-warning-soft text-ink',
    danger: 'border-danger/20 bg-danger-soft text-danger',
  };
  const iconTone = { info: 'text-ink-3', success: 'text-success', warning: 'text-warning', danger: 'text-danger' };
  return (
    <div className={`flex shrink-0 items-start gap-2.5 border-b px-4 py-2 text-[12.5px] leading-relaxed sm:px-6 ${tones[tone]}`} role={live ? 'status' : undefined}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${iconTone[tone]}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">{children}</div>
      {action}
      {onDismiss && (
        <button type="button" onClick={onDismiss} className="-mr-1 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink" aria-label="Dismiss">
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
};

/** Failed or stopped first index: what happened and what to do next. */
const IndexingProblem = ({ repo, onRetry, retrying, onDelete }) => {
  const failed = repo.status === 'failed';
  const repository = parseRepository(repo.github_url);
  const detail = failed
    ? repo.message || 'The indexing worker could not finish this repository.'
    : 'Nothing was made searchable. Start again whenever you are ready; it fetches the latest commit.';
  return (
    <section className={`rounded-2xl border p-5 sm:p-6 ${failed ? 'border-danger/30 bg-surface' : 'border-line bg-surface'}`} aria-labelledby="indexing-problem-title">
      <div className="flex items-start gap-3">
        <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${failed ? 'bg-danger-soft text-danger' : 'bg-surface-2 text-ink-3'}`}>
          {failed ? <AlertTriangle className="h-4.5 w-4.5" aria-hidden="true" /> : <Square className="h-4 w-4" aria-hidden="true" />}
        </span>
        <div className="min-w-0">
          <h2 id="indexing-problem-title" className="text-[15px] font-semibold text-ink">{failed ? 'Indexing failed' : 'Indexing was stopped'}</h2>
          <p className="mt-1 text-sm leading-relaxed text-ink-2">{detail}</p>
          {failed && <p className="mt-2 text-[13px] leading-relaxed text-ink-3">{failureAdvice(repo.message)}</p>}
        </div>
      </div>
      <div className="mt-5 flex flex-wrap items-center gap-2 sm:pl-12">
        <Button variant="primary" size="sm" icon={RefreshCw} loading={retrying} onClick={onRetry}>{failed ? 'Try again' : 'Start indexing again'}</Button>
        {repository && (
          <Button variant="secondary" size="sm" href={`https://github.com/${repository.fullName}`} target="_blank" rel="noreferrer" iconRight={ExternalLink}>Open on GitHub</Button>
        )}
        <Button variant="danger-ghost" size="sm" icon={Trash2} onClick={onDelete}>Delete</Button>
      </div>
    </section>
  );
};

const RepoWorkspace = ({ repo, onRequestDialog }) => {
  const messages = useStore((state) => state.messages);
  const isHistoryLoading = useStore((state) => state.isHistoryLoading);
  const historyError = useStore((state) => state.historyError);
  const retryHistory = useStore((state) => state.retryHistory);
  const isQuerying = useStore((state) => state.isQuerying);
  const askQuestion = useStore((state) => state.askQuestion);
  const stopQuery = useStore((state) => state.stopQuery);
  const clearChat = useStore((state) => state.clearChat);
  const sourcesOpen = useStore((state) => state.sources.open);
  const closeSources = useStore((state) => state.closeSources);
  const reindexRepo = useStore((state) => state.reindexRepo);
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get('tab') === 'overview' ? 'overview' : 'chat';
  const [actionError, setActionError] = useState('');
  const [retrying, setRetrying] = useState(false);
  const [finished, setFinished] = useState(false);
  const [dismissed, setDismissed] = useState(readDismissed);
  const scrollRef = useRef(null);
  const composerRef = useRef(null);
  const tabRefs = useRef({});
  const stickToBottom = useRef(true);
  const repository = parseRepository(repo.github_url);
  const eta = useIndexingEta(repo);
  const notice = location.state?.notice || '';

  // The tab lives in the URL (?tab=overview) so it survives reloads and can be linked.
  const setTab = (next) => {
    if (next === tab) return;
    setSearchParams((current) => {
      const params = new URLSearchParams(current);
      if (next === 'overview') params.set('tab', 'overview');
      else params.delete('tab');
      return params;
    }, { replace: true, state: location.state });
  };

  useEffect(() => {
    setActionError('');
    setFinished(false);
    stickToBottom.current = true;
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [repo.id]);

  // Follow the conversation only when there is one and the reader is near
  // the bottom; progress and empty states always open at the top.
  useEffect(() => {
    const node = scrollRef.current;
    if (!node || tab !== 'chat' || !messages.length) return;
    if (stickToBottom.current) node.scrollTop = node.scrollHeight;
  }, [messages, tab]);
  useEffect(() => {
    const node = scrollRef.current;
    if (node && tab === 'overview') node.scrollTop = 0;
  }, [tab]);

  // Screen readers hear when an answer finishes or fails (the stage
  // indicator's live region unmounts as soon as text starts streaming), and
  // when indexing reaches a new step.
  const [announcement, setAnnouncement] = useState('');
  const lastAssistant = [...messages].reverse().find((message) => message.role === 'assistant');
  const wasStreaming = useRef(false);
  useEffect(() => {
    const streaming = Boolean(lastAssistant?.streaming);
    if (wasStreaming.current && !streaming && lastAssistant) {
      if (lastAssistant.mode === 'error' || lastAssistant.error) setAnnouncement(`The answer failed: ${lastAssistant.error || lastAssistant.content}`);
      else if (lastAssistant.stopped) setAnnouncement('Stopped.');
      else setAnnouncement(lastAssistant.proposal_id || lastAssistant.edit_suggestion ? 'The proposed change is ready to review.' : 'Answer ready.');
    }
    wasStreaming.current = streaming;
  }, [lastAssistant?.streaming, lastAssistant?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const step = phaseIndex(repo);
  const previous = useRef(null);
  useEffect(() => {
    const before = previous.current;
    previous.current = { id: repo.id, step, status: repo.status, queryable: repo.queryable, indexing: repo.is_indexing };
    if (!before || before.id !== repo.id) return;
    if (before.indexing && !repo.is_indexing) {
      if (repo.status === 'failed') setAnnouncement(`Indexing ${repo.repo_name} failed. ${repo.message || ''}`);
      else if (repo.status === 'cancelled') setAnnouncement(`Indexing ${repo.repo_name} stopped.`);
      else {
        setAnnouncement(`${repo.repo_name} is fully indexed.`);
        setFinished(true);
      }
    } else if (!before.queryable && repo.queryable && repo.is_indexing) {
      setAnnouncement('Keyword search is ready. You can ask questions now while the semantic index finishes.');
    } else if (repo.is_indexing && step !== before.step && STEPS[step]) {
      setAnnouncement(`Indexing step ${Math.min(step + 1, 6)} of 6: ${STEPS[step].label}.`);
    }
  }, [repo.id, step, repo.status, repo.queryable, repo.is_indexing]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!finished) return undefined;
    const timer = window.setTimeout(() => setFinished(false), 15000);
    return () => window.clearTimeout(timer);
  }, [finished]);

  // Only one sources panel is mounted: docked on large screens, a sheet below.
  const isLarge = useMediaQuery('(min-width: 1024px)');
  const sheetRef = useRef(null);
  useFocusTrap(sheetRef, sourcesOpen && !isLarge, { onEscape: closeSources });

  const onScroll = () => {
    const node = scrollRef.current;
    if (node) stickToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
  };

  const ask = (question, options) => {
    stickToBottom.current = true;
    setTab('chat');
    void askQuestion(question, options);
  };

  const lastUser = [...messages].reverse().find((message) => message.role === 'user');
  const lastMessage = messages[messages.length - 1];
  const regenerate = lastUser && lastMessage?.role === 'assistant' && !isQuerying
    ? () => ask(lastUser.content, { workflow: lastMessage.workflow || 'general', modelProfile: lastMessage.model_profile || 'fast' })
    : null;

  const runAction = async (action, fallback) => {
    setActionError('');
    try {
      await action();
    } catch (error) {
      setActionError(errorMessage(error, fallback));
    }
  };

  const retry = async () => {
    setRetrying(true);
    await runAction(() => reindexRepo(repo), 'Could not queue a re-index.');
    setRetrying(false);
  };

  const dismissNotice = (key) => {
    rememberDismissed(key);
    setDismissed(readDismissed());
  };
  const clearNavigationNotice = () => navigate(`${location.pathname}${location.search}`, { replace: true, state: null });

  const onTabKeyDown = (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = TABS.findIndex(([value]) => value === tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length;
    setTab(TABS[next][0]);
    tabRefs.current[TABS[next][0]]?.focus();
  };

  const notQueryable = !repo.queryable;
  const problem = notQueryable && (repo.status === 'failed' || repo.status === 'cancelled');
  const stopping = Boolean(repo.is_indexing && repo.message?.startsWith('Stopping'));
  const semantic = Math.max(0, Math.min(100, Number(repo.semantic_progress) || 0));
  const keywordOnly = isKeywordOnly(repo);
  // A finished repository can carry a note from its last run (a stopped or
  // failed refresh, a budget summary, a semantic-search warning).
  const readyNote = !repo.is_indexing && repo.queryable && repo.message ? repo.message : '';
  const readyNoteKey = `${repo.id}:${readyNote}`;
  const readyNoteWarning = /unavailable|fail|timed out|warning|stopped|could not/i.test(readyNote);

  const problemPanel = (
    <IndexingProblem repo={repo} onRetry={retry} retrying={retrying} onDelete={() => onRequestDialog('delete', repo)} />
  );
  const progressPanel = (
    <IngestionProgress repo={repo} onStop={repo.is_indexing ? () => onRequestDialog('stop', repo) : null} stopping={stopping} />
  );

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Header: one row of controls; status and links underneath. */}
        <div className="shrink-0 border-b border-line bg-canvas px-4 py-2.5 sm:px-6 sm:py-3">
          <div className="flex items-center gap-2 sm:gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                <h1 className="truncate text-[15px] font-semibold tracking-tight text-ink" title={repo.repo_name}>{repo.repo_name}</h1>
                <Badge tone={statusTone(repo)} dot className="hidden sm:inline-flex">{phaseLabel(repo)}</Badge>
              </div>
              <div className="mt-0.5 flex min-w-0 items-center gap-x-3 text-[12px] text-ink-3">
                <Badge tone={statusTone(repo)} dot className="sm:hidden">{phaseLabel(repo)}</Badge>
                {repository && (
                  <a href={`https://github.com/${repository.fullName}`} target="_blank" rel="noreferrer" className="inline-flex min-w-0 items-center gap-1 hover:text-ink" title={`Open ${repository.fullName} on GitHub`}>
                    <span className="truncate">{repository.fullName}</span> <ExternalLink className="h-3 w-3 shrink-0" aria-hidden="true" /><span className="sr-only">(opens GitHub)</span>
                  </a>
                )}
                {repo.ref && (
                  <span className="hidden min-w-0 items-center gap-1 font-mono sm:inline-flex" title={`Indexed ${repo.ref_kind || 'ref'} ${repo.ref}`}>
                    {repo.ref_kind === 'tag' ? <Tag className="h-3.5 w-3.5 shrink-0" aria-hidden="true" /> : <GitBranch className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
                    <span className="truncate">{repo.ref}</span>
                  </span>
                )}
                {repo.commit_sha && (
                  <span className="hidden shrink-0 items-center gap-1 font-mono md:inline-flex" title={`Indexed commit ${repo.commit_sha}`}>
                    <GitCommitHorizontal className="h-3.5 w-3.5" aria-hidden="true" /> {shortSha(repo.commit_sha)}
                  </span>
                )}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1 sm:gap-1.5">
              <div className="flex rounded-xl border border-line bg-surface-2 p-0.5" role="tablist" aria-label="Workspace view" onKeyDown={onTabKeyDown}>
                {TABS.map(([value, label, Icon]) => (
                  <button
                    key={value}
                    ref={(node) => { tabRefs.current[value] = node; }}
                    id={`workspace-tab-${value}`}
                    type="button"
                    role="tab"
                    aria-selected={tab === value}
                    aria-controls="workspace-panel"
                    tabIndex={tab === value ? 0 : -1}
                    onClick={() => setTab(value)}
                    className={`inline-flex h-7 items-center gap-1.5 rounded-[10px] px-2.5 text-[12px] font-medium transition-colors ${tab === value ? 'bg-surface text-ink shadow-sm' : 'text-ink-3 hover:text-ink'}`}
                  >
                    <Icon className="hidden h-3.5 w-3.5 sm:block" aria-hidden="true" /> {label}
                  </button>
                ))}
              </div>
              <Button size="icon" variant="ghost" icon={SquarePen} aria-label="New chat" title="New chat" disabled={!messages.length || isQuerying} onClick={() => runAction(clearChat, 'Could not clear the conversation.')} />
              <Menu
                label="Repository actions"
                trigger={({ toggle, props }) => (
                  <button type="button" onClick={toggle} {...props} className="flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink">
                    <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
                  </button>
                )}
                items={[
                  { label: 'Export conversation', icon: Download, disabled: !messages.length, onSelect: () => downloadText(`${slugify(repo.repo_name)}-conversation.md`, exportMarkdown(repo, messages), 'text/markdown') },
                  repo.is_indexing
                    ? { label: 'Stop indexing…', icon: Square, disabled: stopping, onSelect: () => onRequestDialog('stop', repo) }
                    : { label: 'Re-index…', icon: RefreshCw, onSelect: () => onRequestDialog('reindex', repo) },
                  { separator: true },
                  { label: 'Coverage report', icon: PieChart, onSelect: () => onRequestDialog('coverage', repo) },
                  { label: 'Change impact', icon: GitFork, disabled: notQueryable, onSelect: () => onRequestDialog('impact', repo) },
                  { label: 'Rename', icon: Pencil, onSelect: () => onRequestDialog('rename', repo) },
                  { label: 'Delete repository', icon: Trash2, danger: true, onSelect: () => onRequestDialog('delete', repo) },
                ]}
              />
            </div>
          </div>
        </div>

        {repo.is_indexing && repo.queryable && (
          <div className="flex shrink-0 items-center gap-3 border-b border-line bg-surface-2/70 px-4 py-2 text-[12.5px] text-ink-2 sm:px-6">
            <Spinner className="h-3.5 w-3.5 shrink-0 text-accent" />
            <p className="min-w-0 flex-1 truncate">
              {repo.refreshing ? (
                <><span className="font-medium text-ink">Refreshing the index</span><span className="hidden sm:inline"> · answers use the previous version until it finishes</span></>
              ) : (
                <><span className="font-medium text-ink">Keyword search is ready</span> · semantic index {semantic}%{remainingLabel(eta) && <span className="hidden sm:inline"> · {remainingLabel(eta)}</span>}</>
              )}
            </p>
            <ProgressBar value={overallProgress(repo)} className="hidden max-w-32 sm:block" label="Indexing progress" />
            <button type="button" onClick={() => onRequestDialog('stop', repo)} disabled={stopping} className="shrink-0 rounded-md px-2 py-1 font-medium text-ink-2 transition-colors hover:bg-surface-3 hover:text-ink disabled:opacity-50" aria-label={`Stop indexing ${repo.repo_name}`}>
              {stopping ? 'Stopping…' : 'Stop'}
            </button>
          </div>
        )}
        {finished && repo.queryable && !repo.is_indexing && (
          <Notice tone="success" icon={CheckCircle2} onDismiss={() => setFinished(false)}>
            <span className="font-medium">{repo.repo_name} is fully indexed.</span> {keywordOnly ? 'Keyword search is on; semantic search is unavailable for this snapshot.' : `${formatCount(repo.indexed_files)} files are searchable by keyword and by meaning.`}
          </Notice>
        )}
        {notice && (
          <Notice icon={Info} onDismiss={clearNavigationNotice} live>{notice}</Notice>
        )}
        {readyNote && !dismissed.has(readyNoteKey) && (
          <Notice
            tone={readyNoteWarning ? 'warning' : 'info'}
            icon={readyNoteWarning ? AlertTriangle : Info}
            onDismiss={() => dismissNotice(readyNoteKey)}
            action={readyNoteWarning ? <Button size="xs" variant="secondary" icon={RefreshCw} onClick={() => onRequestDialog('reindex', repo)}>Re-index</Button> : null}
          >
            {readyNote}
          </Notice>
        )}
        {actionError && (
          <Notice tone="danger" icon={AlertTriangle} onDismiss={() => setActionError('')}>
            <span role="alert">{actionError}</span>
          </Notice>
        )}

        {/* Body */}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          id="workspace-panel"
          role="tabpanel"
          aria-labelledby={`workspace-tab-${tab}`}
          className="scroll-thin min-h-0 flex-1 overflow-y-auto"
        >
          <div className={`mx-auto w-full px-4 py-6 sm:px-6 ${tab === 'overview' ? 'max-w-4xl' : 'max-w-3xl'}`}>
            {problem ? problemPanel : tab === 'overview' ? (
              notQueryable ? progressPanel : (
                <RepoOverview
                  repo={repo}
                  onAsk={(question) => ask(question, { workflow: 'general', modelProfile: 'fast' })}
                  onOpenCoverage={() => onRequestDialog('coverage', repo)}
                />
              )
            ) : notQueryable ? progressPanel : isHistoryLoading ? (
              <div className="space-y-6 py-4" aria-busy="true">
                <span className="sr-only" role="status">Loading conversation</span>
                {[0, 1].map((index) => (
                  <div key={index} className="space-y-2.5" aria-hidden="true">
                    <div className="skeleton ml-auto h-9 w-2/5 rounded-2xl" />
                    <div className="skeleton h-4 w-11/12" />
                    <div className="skeleton h-4 w-4/5" />
                    <div className="skeleton h-4 w-3/5" />
                  </div>
                ))}
              </div>
            ) : (
              <>
                {historyError && (
                  <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3 text-[13px] text-danger" role="alert">
                    <span>{historyError}</span>
                    <button type="button" onClick={retryHistory} className="shrink-0 font-semibold underline underline-offset-2">Retry</button>
                  </div>
                )}
                {messages.length === 0 ? (
                  <div className="animate-rise py-2 sm:py-4">
                    <p className="eyebrow">Ask anything</p>
                    <h2 className="mt-2 text-xl font-semibold tracking-tight text-ink sm:text-2xl">What do you want to know about {repo.repo_name}?</h2>
                    <p className="mt-2 text-sm leading-relaxed text-ink-3">
                      {repo.is_indexing && !repo.refreshing
                        ? <>Keyword search is ready, so you can ask now. The semantic index is {semantic}% built; when it finishes, answers get better at finding code by meaning.</>
                        : <>Answers cite exact files and lines. Switch to <span className="font-medium text-ink-2">Edit &amp; PR</span> to have a change written, validated and turned into a pull request.</>}
                    </p>
                    <div className="mt-6">
                      <RepoOverview
                        repo={repo}
                        compact
                        onAsk={(question) => ask(question, { workflow: 'general', modelProfile: 'fast' })}
                        onShowOverview={() => setTab('overview')}
                      />
                    </div>
                  </div>
                ) : (
                  <div className="space-y-7" role="log" aria-label="Conversation" aria-live="off">
                    {messages.map((message, index) => (
                      <MessageBubble key={message.id} message={message} repo={repo} isLast={index === messages.length - 1} onRegenerate={index === messages.length - 1 ? regenerate : null} />
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </div>

        <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>

        <div className="shrink-0 border-t border-line bg-canvas px-3 pb-3 pt-3 sm:px-6">
          <Composer
            ref={composerRef}
            busy={isQuerying}
            disabled={notQueryable || isHistoryLoading}
            disabledReason={repo.status === 'failed' ? 'Indexing failed. Try again above to ask questions.' : repo.status === 'cancelled' && notQueryable ? 'Indexing was stopped. Start it again to ask questions.' : undefined}
            onSubmit={ask}
            onStop={stopQuery}
          />
        </div>
      </div>

      {/* Sources: docked on large screens, sheet on small screens */}
      {sourcesOpen && (isLarge ? (
        <div className="flex w-[min(46vw,42rem)] shrink-0 flex-col border-l border-line">
          <SourcesPanel repo={repo} onClose={closeSources} />
        </div>
      ) : (
        <div className="fixed inset-0 z-[70] flex flex-col bg-black/40 pt-12" role="dialog" aria-modal="true" aria-label="Sources">
          <div ref={sheetRef} tabIndex={-1} className="flex min-h-0 flex-1 animate-rise flex-col overflow-hidden rounded-t-3xl border-t border-line bg-surface outline-none">
            <SourcesPanel repo={repo} onClose={closeSources} />
          </div>
        </div>
      ))}
    </div>
  );
};

export default RepoWorkspace;
