import { ChevronDown, ChevronRight, Download, FilePlus2, FileDiff, GitPullRequest } from 'lucide-react';
import { useState } from 'react';
import { basename, compactPath, dirname } from '../../lib/citations';
import { downloadText, proposalPatch, slugify } from '../../lib/patch';
import useStore from '../../store/useStore';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import DiffView from './DiffView';
import { approximatePaths, issueLabel } from '../../lib/proposal';
import ProposalNotices from './ProposalNotices';

const FileRow = ({ file, open, onToggle, approximate }) => {
  const created = file.action === 'create';
  const Icon = created ? FilePlus2 : FileDiff;
  return (
    <button type="button" onClick={onToggle} aria-expanded={open} className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left transition-colors hover:bg-surface-2">
      {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-ink-3" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-3" />}
      <Icon className={`h-4 w-4 shrink-0 ${created ? 'text-success' : 'text-ink-3'}`} aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]" title={file.file_path}>
        <span className="text-ink">{basename(file.file_path)}</span>
        {dirname(file.file_path) && <span className="ml-1.5 text-[11.5px] text-ink-3">{compactPath(dirname(file.file_path), 36)}</span>}
      </span>
      {created && <Badge tone="success">New</Badge>}
      {approximate && <span title="Reconstructed from the index; refresh from GitHub before pushing."><Badge tone="info">Approximate</Badge></span>}
      <span className="shrink-0 font-mono text-[11.5px]">
        <span className="text-success">+{file.additions ?? 0}</span>{' '}
        <span className="text-danger">−{file.deletions ?? 0}</span>
      </span>
    </button>
  );
};

/** Inline summary of a validated patch with expandable per-file diffs. */
const EditProposalCard = ({ message }) => {
  const openReview = useStore((state) => state.openReview);
  const selectedRepo = useStore((state) => state.selectedRepo);
  const proposal = message.edit_suggestion;
  const [openFiles, setOpenFiles] = useState(() => new Set(proposal?.files?.[0] ? [proposal.files[0].file_path] : []));

  if (!proposal) {
    return (
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-line bg-surface px-4 py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <GitPullRequest className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
          <span className="truncate text-sm font-medium text-ink">{message.proposal_title || 'Proposed change'}</span>
        </div>
        <Button size="sm" variant="primary" icon={FileDiff} onClick={() => openReview(message.id)}>Open review</Button>
      </div>
    );
  }

  const files = proposal.files || [];
  const approximate = approximatePaths(proposal);
  const stats = proposal.stats || {};
  const patch = () => proposalPatch(proposal);
  const canDownload = files.every((file) => file.after != null);

  return (
    <div className="mt-4 overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-soft)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <GitPullRequest className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
            <p className="truncate text-sm font-semibold text-ink">{proposal.title || 'Proposed change'}</p>
          </div>
          <p className="mt-0.5 text-[12px] text-ink-3">
            {files.length} file{files.length === 1 ? '' : 's'} changed ·{' '}
            <span className="text-success">+{stats.additions ?? 0}</span>{' '}
            <span className="text-danger">−{stats.deletions ?? 0}</span>
            {proposal.issue_reference ? ` · ${issueLabel(proposal)}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {canDownload && (
            <Button size="sm" variant="ghost" icon={Download} onClick={() => downloadText(`${slugify(selectedRepo)}-${slugify(proposal.title)}.patch`, patch())}>
              .patch
            </Button>
          )}
          <Button size="sm" variant="primary" icon={GitPullRequest} onClick={() => openReview(message.id)}>
            Review &amp; create PR
          </Button>
        </div>
      </div>
      <ProposalNotices proposal={proposal} className="border-b border-line px-4 py-3" />
      <div className="divide-y divide-line">
        {files.map((file) => {
          const open = openFiles.has(file.file_path);
          return (
            <div key={file.file_path}>
              <FileRow
                file={file}
                approximate={approximate.has(file.file_path)}
                open={open}
                onToggle={() => setOpenFiles((current) => {
                  const next = new Set(current);
                  if (next.has(file.file_path)) next.delete(file.file_path);
                  else next.add(file.file_path);
                  return next;
                })}
              />
              {open && (
                <div className="px-3.5 pb-3.5">
                  {file.summary && <p className="mb-2 text-[12.5px] text-ink-3">{file.summary}</p>}
                  {file.after != null ? (
                    <DiffView before={file.action === 'create' ? '' : file.before} after={file.after} />
                  ) : (
                    <p className="rounded-xl border border-line bg-surface-2 p-3 text-[12.5px] text-ink-3">This file is large; open the review to load the full diff.</p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default EditProposalCard;
