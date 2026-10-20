import { Download, FileDiff, GitPullRequest } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import { buildToolChips } from '../../lib/ai/toolChips';
import { downloadText, proposalPatch, slugify } from '../../lib/patch';
import { issueLabel } from '../../lib/proposal';
import useArtifacts, {
  PROPOSAL_PANEL_ID, artifactCardId, proposalArtifactStatus, proposalArtifactSummary,
} from '../../store/useArtifacts';
import useStore from '../../store/useStore';
import { ArtifactCard } from '../ai/ArtifactPanel';
import { ToolChips } from '../ai/ToolChips';
import Button from '../ui/Button';
import ProposalNotices from './ProposalNotices';

/*
 * The code-change part of an Edit & PR answer:
 * - `ProposalToolChips`: the editor's run (its stored `tool_calls`, a failed
 *   validation as an error call) and one chip per changed file with the diff
 *   of its real hunks in a popover. Rendered above the answer text.
 * - `EditProposalCard` (default): the proposal's warnings, then an Artifact
 *   Card that opens the proposal in the Artifact Panel (useArtifacts), next
 *   to the review / pull request entry point and the .patch download.
 */

/**
 * An Edit & PR answer: live ones carry `workflow`, stored ones only `mode`
 * (or a proposal id from their code_edit call).
 *
 * @param {object} message
 */
export const isEditRun = (message) => message?.role === 'assistant' && (
  message.workflow === 'editing' || message.mode === 'editing' || Boolean(message.edit_suggestion || message.proposal_id)
);

const hasProposal = (message) => Boolean(message.edit_suggestion || message.proposal_id);

/** The one name of the review / pull request action (beside the card and in the panel). */
export const REVIEW_LABEL = 'Review & open PR';

/**
 * The run ended without a proposal: the editor explained why in the answer
 * ("I couldn't produce a safe patch …").
 *
 * @param {object} message
 */
const validationFailed = (message) => !message.streaming && message.mode === 'editing' && !hasProposal(message);

const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

/**
 * The card's note: "2 files · +2 −1", plus "· Refs #7" when the change names
 * an issue. (Tool Chips above already say "N files changed".)
 */
const describeProposal = (proposal) => {
  if (!proposal) return undefined;
  const files = proposal.files || [];
  const stats = proposal.stats || {};
  const count = Number.isFinite(stats.files) ? stats.files : files.length;
  const added = stats.additions ?? files.reduce((sum, file) => sum + (file.additions || 0), 0);
  const removed = stats.deletions ?? files.reduce((sum, file) => sum + (file.deletions || 0), 0);
  const issue = issueLabel(proposal);
  return `${plural(count, 'file')} · +${added} −${removed}${issue ? ` · ${issue}` : ''}`;
};

/** Why a finished run has no change (the card's error note). */
const errorNote = (message) => {
  if (message.mode === 'editing') return 'No safe patch was produced.';
  if (message.mode === 'retrieval_fallback') return 'The model was unavailable, so no change was written.';
  return undefined;
};

/**
 * The editor's tool calls and changed files as Tool Chips; nothing when the
 * run has neither (a stopped run, or a stored answer from an older server).
 *
 * @param {{ message: object, className?: string }} props
 */
export const ProposalToolChips = ({ message, className }) => {
  const proposal = message.edit_suggestion || null;
  const failure = validationFailed(message) ? message.content : null;
  const { summary, calls, changes } = useMemo(
    () => buildToolChips({ toolCalls: message.tool_calls, proposal, failure }),
    [message.tool_calls, proposal, failure],
  );
  if (!calls.length && !changes.length) return null;
  return <ToolChips summary={summary} calls={calls} changes={changes} className={className} />;
};

/**
 * Warnings, the Artifact Card for the proposal, and the review entry point.
 *
 * @param {{ message: object }} props
 */
const EditProposalCard = ({ message }) => {
  const openReview = useStore((state) => state.openReview);
  const loadProposal = useStore((state) => state.loadProposal);
  const selectedRepo = useStore((state) => state.selectedRepo);
  const isOpen = useArtifacts((state) => state.artifact?.kind === 'proposal' && state.artifact.messageId === message.id);
  const openProposal = useArtifacts((state) => state.openProposal);
  const closeArtifact = useArtifacts((state) => state.close);

  const proposal = message.edit_suggestion || null;
  const status = proposalArtifactStatus(message);
  const { title } = proposalArtifactSummary(message);
  const cardId = artifactCardId(message.id);
  const files = proposal?.files || [];
  const canDownload = files.length > 0 && files.every((file) => file.after != null);
  const reviewable = status === 'complete';
  const note = status === 'error' ? errorNote(message) : undefined;

  // A reply from history carries only its proposal's id. Load the proposal
  // once the reply comes into view, so its file chips and the card's note
  // are there without opening the panel (one request per proposal; a failed
  // load is tried again when the panel opens).
  const rootRef = useRef(null);
  const needsProposal = status === 'complete' && !proposal && Boolean(message.proposal_id);
  useEffect(() => {
    const node = rootRef.current;
    if (!needsProposal || !node) return undefined;
    const load = () => { loadProposal(message.id).catch(() => {}); };
    if (typeof IntersectionObserver !== 'function') {
      load();
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      load();
    }, { rootMargin: '200px 0px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [needsProposal, message.id, loadProposal]);

  return (
    <div ref={rootRef} data-slot="proposal" className="mt-4 space-y-3">
      {proposal && <ProposalNotices proposal={proposal} />}
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 max-w-sm flex-1 basis-64">
          <ArtifactCard
            id={cardId}
            title={title}
            description={describeProposal(proposal)}
            status={status}
            isOpen={isOpen}
            icon={<FileDiff />}
            labels={note ? { error: note } : undefined}
            aria-controls={isOpen ? PROPOSAL_PANEL_ID : undefined}
            // Pressing the card of the open panel closes it (aria-expanded).
            onClick={() => (isOpen ? closeArtifact() : openProposal(message.id, cardId))}
          />
        </div>
        {reviewable && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="primary" icon={GitPullRequest} onClick={() => openReview(message.id)}>
              {REVIEW_LABEL}
            </Button>
            {canDownload && (
              <Button
                size="sm"
                variant="ghost"
                icon={Download}
                onClick={() => downloadText(`${slugify(selectedRepo)}-${slugify(proposal.title)}.patch`, proposalPatch(proposal))}
              >
                .patch
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default EditProposalCard;
