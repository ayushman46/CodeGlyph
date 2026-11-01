import { LogOut, Users } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { errorMessage, leaveRepository } from '../../api/client';
import { toast } from '../../hooks/useToasts';
import { ROLE_DESCRIPTIONS, ROLE_LABELS, canManage, editBlockedReason, ownerName, roleOf } from '../../lib/sharing';
import useStore from '../../store/useStore';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import ShareDialog from './ShareDialog';

/**
 * A member's view of a shared repository: who shared it, their role, and
 * Leave. Leaving from inside the repository returns to the workspace;
 * `stay` keeps the current page (the account page).
 */
export const AccessDialog = ({ repo, open, onClose, stay = false }) => {
  const fetchRepos = useStore((state) => state.fetchRepos);
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const role = roleOf(repo);
  const owner = ownerName(repo);

  const leave = async () => {
    setBusy(true);
    setError('');
    try {
      await leaveRepository(repo.repo_name);
      onClose();
      if (!stay) navigate('/app', { replace: true });
      toast(`You left ${repo.repo_name}`, { description: `Ask ${owner} for a new invite link to rejoin.` });
      await fetchRepos();
    } catch (leaveError) {
      setError(errorMessage(leaveError, 'You could not leave this repository. Try again.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => { setConfirming(false); onClose(); }}
      busy={busy}
      size="sm"
      title="Shared with you"
      description={`${owner} shared ${repo.repo_name} with you. You ask questions using their index; your conversations stay private to you.`}
      // Keys keep React from reusing (and colour-animating) the other state's buttons.
      footer={confirming ? (
        <>
          <Button key="cancel" variant="ghost" size="sm" onClick={() => setConfirming(false)} disabled={busy}>Cancel</Button>
          <Button key="leave" variant="danger" size="sm" loading={busy} onClick={leave}>Leave repository</Button>
        </>
      ) : (
        <>
          <Button key="ask-leave" variant="danger-ghost" size="sm" icon={LogOut} className="mr-auto" onClick={() => setConfirming(true)}>Leave…</Button>
          <Button key="done" variant="secondary" size="sm" onClick={onClose}>Done</Button>
        </>
      )}
    >
      <div className="space-y-3 text-[13px] leading-relaxed text-ink-2">
        <p className="flex flex-wrap items-center gap-2">
          Your access: <Badge tone={role === 'editor' ? 'accent' : 'neutral'}>{ROLE_LABELS[role]}</Badge>
        </p>
        <p>{ROLE_DESCRIPTIONS[role]}</p>
        {editBlockedReason(repo) && <p className="text-ink-3">Need Edit &amp; PR? Ask {owner} to make you an editor.</p>}
        <p className="text-ink-3">Renaming, re-indexing, deleting and sharing are up to {owner}.</p>
        {confirming && (
          <p className="rounded-xl bg-danger-soft px-3 py-2.5 text-danger">
            Leave {repo.repo_name}? It disappears from your workspace, and you need a new invite link to come back.
          </p>
        )}
        {error && <p className="text-danger" role="alert">{error}</p>}
      </div>
    </Modal>
  );
};

/**
 * Header control for a repository's sharing: "Share" for its owner (with
 * the member count), the member's role for a shared one.
 */
const RepoAccessButton = ({ repo }) => {
  const [open, setOpen] = useState(false);
  if (!repo) return null;
  const owner = canManage(repo);
  const count = Math.max(0, Number(repo.member_count) || 0);
  return (
    <>
      {owner ? (
        <Button
          size="sm"
          variant="ghost"
          icon={Users}
          onClick={() => setOpen(true)}
          aria-label={count ? `Share ${repo.repo_name} (shared with ${count})` : `Share ${repo.repo_name}`}
          title="Share with teammates"
        >
          <span className="hidden sm:inline">Share</span>
          {count > 0 && <span className="font-mono text-[11px] text-ink-3" aria-hidden="true">{count}</span>}
        </Button>
      ) : (
        <Button
          size="sm"
          variant="ghost"
          icon={Users}
          onClick={() => setOpen(true)}
          aria-label={`Shared with you by ${ownerName(repo)}: ${ROLE_LABELS[roleOf(repo)]} access`}
          title={`Shared with you by ${ownerName(repo)}`}
        >
          <span className="hidden sm:inline">{ROLE_LABELS[roleOf(repo)]}</span>
        </Button>
      )}
      {owner
        ? <ShareDialog repo={repo} open={open} onClose={() => setOpen(false)} />
        : <AccessDialog repo={repo} open={open} onClose={() => setOpen(false)} />}
    </>
  );
};

export default RepoAccessButton;
