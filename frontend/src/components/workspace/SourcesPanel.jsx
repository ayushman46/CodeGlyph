import { ArrowLeft, FileCode2, Folder } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import { basename, formatLines, groupByDirectory, partitionCitations } from '../../lib/citations';
import useStore from '../../store/useStore';
import { ArtifactPanel } from '../ai/ArtifactPanel';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import { mergeRefs } from '../ui/Popover';
import FileViewer from './FileViewer';

const METHOD_LABELS = {
  semantic: 'Semantic',
  keyword: 'Keyword',
  workflow_target: 'Path match',
  requested_file: 'Named file',
  dependency: 'Import graph',
  editing_target_file: 'Edit target',
  issue_support: 'Issue match',
  conversation: 'Follow-up',
  overview: 'Overview',
};

const CitationRow = ({ citation, onOpen, active, muted }) => {
  const ref = useRef(null);
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: 'center' });
  }, [active]);
  const range = formatLines(citation.start_line, citation.end_line);
  return (
    <button
      ref={ref}
      type="button"
      onClick={onOpen}
      aria-label={`Open ${citation.file_path}${range ? ` at ${range}` : ''}`}
      className={`group flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition-colors hover:bg-surface-2 ${active ? 'bg-accent-soft/60 ring-1 ring-accent/30' : ''}`}
    >
      <FileCode2 className={`mt-0.5 h-4 w-4 shrink-0 group-hover:text-accent ${muted ? 'text-ink-3/70' : 'text-ink-3'}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className={`truncate font-mono text-[12.5px] font-medium ${muted ? 'text-ink-2' : 'text-ink'}`} title={citation.file_path}>{basename(citation.file_path)}</span>
          {range && <span className="shrink-0 font-mono text-[11px] text-ink-3">{range}</span>}
        </div>
        {(citation.retrieval_methods || []).length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {(citation.retrieval_methods || []).slice(0, 3).map((method) => (
              <Badge key={method} tone="neutral">{METHOD_LABELS[method] || method}</Badge>
            ))}
          </div>
        )}
        {citation.retrieval_reasons?.[0] && (
          <p className="mt-1 line-clamp-2 text-[11.5px] leading-snug text-ink-3">{citation.retrieval_reasons[0]}</p>
        )}
      </div>
    </button>
  );
};

const FolderGroups = ({ items, onOpen, activeIndex, muted }) => groupByDirectory(items).map((group) => (
  <section key={group.directory || '(root)'} className="mb-2">
    <div className="flex items-center gap-1.5 px-3 pb-0.5 pt-1.5">
      <Folder className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />
      <h4 className="truncate font-mono text-[11.5px] font-medium text-ink-2" title={group.directory || 'repository root'}>
        {group.directory ? `${group.directory}/` : 'repository root'}
      </h4>
    </div>
    {group.items.map((citation) => (
      <CitationRow
        key={`${citation.file_path}-${citation.start_line}-${citation.index}`}
        citation={citation}
        active={activeIndex === citation.index}
        muted={muted}
        onOpen={() => onOpen(citation)}
      />
    ))}
  </section>
));

/** The list body's scrollbar matches the rest of the app (thin, themed). */
const LIST_BODY = '[&>[data-slot=artifact-panel-body]]:[scrollbar-width:thin] [&>[data-slot=artifact-panel-body]]:[scrollbar-color:var(--color-line-strong)_transparent]';

/**
 * Panel beside the conversation: sources grouped by folder, or one file at
 * the cited lines. Both views are framed as an Artifact Panel (not a
 * dialog: no focus trap); `autoFocus` moves focus in when the panel opens,
 * and moving between the list and a file keeps focus in the panel.
 */
const SourcesPanel = ({ repo, onClose, autoFocus = false, panelRef }) => {
  const sources = useStore((state) => state.sources);
  const messages = useStore((state) => state.messages);
  const openFile = useStore((state) => state.openFile);
  const backToSources = useStore((state) => state.backToSources);
  const messageIndex = messages.findIndex((item) => item.id === sources.messageId);
  const message = messageIndex >= 0 ? messages[messageIndex] : null;
  const question = useMemo(() => {
    for (let index = messageIndex - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'user') return messages[index].content;
    }
    return '';
  }, [messages, messageIndex]);
  const citations = useMemo(() => message?.citations || [], [message]);
  const { cited, retrieved, marked } = useMemo(() => partitionCitations(citations), [citations]);
  const folderCount = useMemo(() => groupByDirectory(citations).length, [citations]);
  const snapshot = message?.snapshot || {};
  const commitSha = snapshot.commit_sha || repo?.commit_sha;
  const githubUrl = snapshot.github_url || repo?.github_url;
  const asideRef = useRef(null);
  const sectionRef = useRef(null);
  const setPanelRef = useMemo(() => mergeRefs(sectionRef, panelRef), [panelRef]);
  // Only the panel that appears when the sources panel opens takes focus by
  // itself; later views follow the rule below.
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; }, []);
  const focusOnMount = autoFocus && !mounted.current;

  const file = sources.file;
  const stack = sources.stack || [];
  const previous = stack[stack.length - 1];
  const canGoBack = Boolean(file) && (stack.length > 0 || citations.length > 0);
  const backLabel = previous?.file ? `Back to ${basename(previous.file.path)}` : 'Back to sources';

  // Other ranges the same answer cites in the open file.
  const fileRanges = useMemo(() => {
    if (!file) return [];
    const seen = new Set();
    return citations
      .filter((citation) => citation.file_path === file.path && citation.start_line)
      .map((citation) => ({ start: citation.start_line, end: citation.end_line || citation.start_line }))
      .filter((range) => {
        const key = `${range.start}-${range.end}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }, [citations, file]);

  // Moving between the list and a file inside the panel keeps keyboard focus
  // in the panel instead of dropping it on the page.
  const view = file ? `${file.path}:${file.start}-${file.end}` : 'list';
  const lastView = useRef(view);
  useEffect(() => {
    if (lastView.current === view) return;
    lastView.current = view;
    const activeElement = document.activeElement;
    if (!activeElement || activeElement === document.body || asideRef.current?.contains(activeElement)) sectionRef.current?.focus();
  }, [view]);

  const open = (citation) => openFile({ path: citation.file_path, start: citation.start_line || null, end: citation.end_line || null });

  const announcement = file
    ? `Showing ${file.path}${file.start ? `, lines ${file.start} to ${file.end || file.start}` : ''}.`
    : '';

  return (
    <aside ref={asideRef} className="relative min-h-0 flex-1" aria-label={file ? `File ${file.path}` : 'Sources'}>
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
      {file ? (
        <FileViewer
          key={view}
          repoName={repo?.repo_name}
          path={file.path}
          startLine={file.start}
          endLine={file.end}
          ranges={fileRanges}
          githubUrl={githubUrl}
          commitSha={commitSha}
          onClose={onClose}
          autoFocus={focusOnMount}
          panelRef={setPanelRef}
          navigation={canGoBack ? (
            <Button variant="link" size="icon-sm" onClick={backToSources} aria-label={backLabel} title={backLabel}>
              <ArrowLeft aria-hidden="true" />
            </Button>
          ) : null}
        />
      ) : (
        <ArtifactPanel
          ref={setPanelRef}
          title="Sources"
          description={citations.length ? `${citations.length} ${citations.length === 1 ? 'section' : 'sections'} · ${folderCount} ${folderCount === 1 ? 'folder' : 'folders'}` : undefined}
          onClose={onClose}
          autoFocus={focusOnMount}
          flush
          className={LIST_BODY}
        >
          {citations.length === 0 ? (
            <div className="p-6 text-sm leading-relaxed text-ink-3">
              {message ? 'This answer has no source citations.' : 'Ask a question to see the code its answer is based on.'}
            </div>
          ) : (
            <div className="px-2 pb-4">
              {question && (
                <p className="mx-3 mt-3 line-clamp-2 border-l-2 border-line-strong pl-2.5 text-[12.5px] leading-snug text-ink-3" title={question}>
                  {question}
                </p>
              )}
              {marked ? (
                <>
                  <section aria-labelledby="sources-cited" className="mt-3">
                    <h3 id="sources-cited" className="flex items-center gap-2 px-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-ink-2">
                      Cited in the answer <span className="font-medium normal-case tracking-normal text-ink-3">{cited.length}</span>
                    </h3>
                    {cited.length
                      ? <FolderGroups items={cited} onOpen={open} activeIndex={sources.activeIndex} />
                      : <p className="px-3 py-2 text-[12.5px] text-ink-3">The answer does not cite these sections directly.</p>}
                  </section>
                  {retrieved.length > 0 && (
                    <section aria-labelledby="sources-retrieved" className="mt-4 border-t border-line pt-3">
                      <h3 id="sources-retrieved" className="flex items-center gap-2 px-3 text-[11px] font-semibold uppercase tracking-wide text-ink-2">
                        Also retrieved <span className="font-medium normal-case tracking-normal text-ink-3">{retrieved.length}</span>
                      </h3>
                      <p className="px-3 pb-1 pt-0.5 text-[11.5px] text-ink-3">Read as evidence but not referenced in the answer.</p>
                      <FolderGroups items={retrieved} onOpen={open} activeIndex={sources.activeIndex} muted />
                    </section>
                  )}
                </>
              ) : (
                <div className="mt-3"><FolderGroups items={cited} onOpen={open} activeIndex={sources.activeIndex} /></div>
              )}
            </div>
          )}
        </ArtifactPanel>
      )}
    </aside>
  );
};

export default SourcesPanel;
