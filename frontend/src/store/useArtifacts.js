import { create } from 'zustand';
import useStore from './useStore';

/*
 * Which artifact the workspace shows beside the conversation (an Artifact
 * Panel), shared by the card that opens it (in a reply) and the workspace
 * that renders it.
 *
 * State:
 *   artifact: null | { kind: 'proposal', messageId: string, openedBy: 'person' | 'reply' }
 *   returnFocusId: id of the element focus returns to when the panel closes
 *     (the Artifact Card that opened it); kept after close() so the panel's
 *     onClose can still read it.
 *
 * The panel shares its slot with the sources panel (useStore().sources):
 * opening a proposal closes the sources panel and opening sources closes the
 * proposal, so only one is ever shown. Switching repositories, clearing the
 * chat or a retry that replaces the proposal's message closes it too.
 */

/** id of the proposal Artifact Panel (for the card's aria-controls). */
export const PROPOSAL_PANEL_ID = 'proposal-artifact-panel';

/** Stable id for the Artifact Card of a reply, so focus can return to it. */
export const artifactCardId = (messageId) => `artifact-card-${messageId}`;

/**
 * Artifact status of an Edit & PR reply: streaming while it is written,
 * complete once it carries a proposal, error when it finished without one
 * (failed, stopped, cut off or not validated).
 *
 * @param {object | null | undefined} message
 * @returns {'streaming' | 'complete' | 'error'}
 */
export const proposalArtifactStatus = (message) => {
  if (message?.streaming) return 'streaming';
  if (message && (message.edit_suggestion || message.proposal_id)) return 'complete';
  return 'error';
};

/**
 * Title and description shared by the proposal's card and panel.
 * Description: "2 files changed · +12 −3" when complete, the live stage
 * while streaming, nothing on error (the status text says it).
 *
 * @param {object | null | undefined} message
 * @returns {{ title: string, description: string | undefined }}
 */
export const proposalArtifactSummary = (message) => {
  const proposal = message?.edit_suggestion;
  const title = proposal?.title || message?.proposal_title || 'Proposed change';
  const status = proposalArtifactStatus(message);
  if (status === 'streaming') return { title, description: message?.stage?.detail || undefined };
  if (status === 'error' || !proposal) return { title, description: undefined };
  const files = Array.isArray(proposal.files) ? proposal.files.length : 0;
  const stats = proposal.stats || {};
  const additions = stats.additions ?? (proposal.files || []).reduce((sum, file) => sum + (file.additions ?? 0), 0);
  const deletions = stats.deletions ?? (proposal.files || []).reduce((sum, file) => sum + (file.deletions ?? 0), 0);
  return { title, description: `${files} file${files === 1 ? '' : 's'} changed · +${additions} −${deletions}` };
};

const useArtifacts = create((set) => ({
  artifact: null,
  returnFocusId: null,

  /**
   * Show a reply's proposal in the Artifact Panel.
   * @param {string} messageId
   * @param {string | null} [returnFocusId] Element focused again on close (the card).
   * @param {{ openedBy?: 'person' | 'reply' }} [options] 'reply' opens it without moving focus.
   */
  openProposal: (messageId, returnFocusId = null, { openedBy = 'person' } = {}) => {
    if (!messageId) return;
    set({ artifact: { kind: 'proposal', messageId, openedBy }, returnFocusId: returnFocusId ?? null });
    if (useStore.getState().sources.open) useStore.getState().closeSources();
  },

  close: () => set((state) => (state.artifact ? { artifact: null } : {})),
}));

// One slot beside the conversation: sources opening replaces the proposal;
// a repository switch, a cleared chat or a replaced message closes it.
useStore.subscribe((state, previous) => {
  const { artifact, close } = useArtifacts.getState();
  if (!artifact) return;
  if (state.sources !== previous.sources && state.sources.open) close();
  else if (state.selectedRepo !== previous.selectedRepo) close();
  else if (state.messages !== previous.messages && !state.messages.some((message) => message.id === artifact.messageId)) close();
});

export default useArtifacts;
