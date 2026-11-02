import { ArrowRight, FolderGit2, Link2Off, Users } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { acceptInvite, errorMessage, previewInvite } from '../api/client';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { toast } from '../hooks/useToasts';
import { ROLE_DESCRIPTIONS, ROLE_LABELS, formatDate, inviteProblem } from '../lib/sharing';
import useStore from '../store/useStore';

const repoPath = (name) => `/app/r/${encodeURIComponent(name)}`;

const Shell = ({ children }) => (
  <main id="main" className="content-shell flex min-h-[calc(100dvh-3.5rem)] items-center justify-center py-16">
    <div className="w-full max-w-md rounded-3xl border border-line bg-surface p-6 shadow-[var(--shadow-soft)] sm:p-8">{children}</div>
  </main>
);

const Problem = ({ problem }) => (
  <Shell>
    <div role="alert">
      <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2 text-ink-3"><Link2Off className="h-5 w-5" aria-hidden="true" /></span>
      <h1 className="mt-4 text-lg font-semibold text-ink">{problem.title}</h1>
      <p className="mt-2 text-sm leading-relaxed text-ink-2">{problem.detail}</p>
    </div>
    <Button className="mt-6" variant="secondary" size="sm" icon={FolderGit2} to="/app">Go to your workspace</Button>
  </Shell>
);

/**
 * /invite/<token>: what the link shares (repository, who shared it, the
 * role) and a Join button. The repository then opens in the workspace.
 */
const InvitePage = () => {
  const { token = '' } = useParams();
  const navigate = useNavigate();
  const fetchRepos = useStore((state) => state.fetchRepos);
  const [invite, setInvite] = useState(null);
  const [problem, setProblem] = useState(null);
  const [joining, setJoining] = useState(false);
  const [joinError, setJoinError] = useState('');
  useDocumentTitle(invite ? `Join ${invite.repository}` : 'Invitation');

  useEffect(() => {
    let active = true;
    setInvite(null);
    setProblem(null);
    previewInvite(token)
      .then((response) => { if (active) setInvite(response.data); })
      .catch((error) => {
        if (!active) return;
        const detail = typeof error?.response?.data?.detail === 'string' ? error.response.data.detail : '';
        setProblem(inviteProblem(error?.response?.status || 0, error?.response?.headers?.['x-error-code'], detail || (error?.response ? '' : errorMessage(error))));
      });
    return () => { active = false; };
  }, [token]);

  const join = async () => {
    setJoining(true);
    setJoinError('');
    try {
      const response = await acceptInvite(token);
      await fetchRepos();
      toast.success(`You joined ${response.data.repo_name}`, { description: 'It is in your sidebar under Shared with you.' });
      navigate(repoPath(response.data.repo_name), { replace: true });
    } catch (error) {
      const code = error?.response?.headers?.['x-error-code'];
      if (code) setProblem(inviteProblem(error.response.status, code, error.response.data?.detail));
      else setJoinError(errorMessage(error, 'You could not join right now. Try again.'));
    } finally {
      setJoining(false);
    }
  };

  if (problem) return <Problem problem={problem} />;
  if (!invite) {
    return (
      <Shell>
        <div role="status" aria-label="Loading the invitation">
          <div className="skeleton h-11 w-11 rounded-xl" />
          <div className="skeleton mt-4 h-6 w-2/3" />
          <div className="skeleton mt-3 h-4 w-full" />
          <div className="skeleton mt-6 h-10 w-full rounded-xl" />
        </div>
      </Shell>
    );
  }
  if (invite.status === 'removed') {
    return <Problem problem={inviteProblem(403, 'share_removed', `${invite.owner_label || 'The owner'} removed your access to ${invite.repository} after this link was created. Ask them for a new link.`)} />;
  }

  const hasAccess = invite.status === 'owner' || invite.status === 'member';
  const shownRole = invite.status === 'member' && invite.access_role ? invite.access_role : invite.role;
  return (
    <Shell>
      <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-soft text-accent-strong"><Users className="h-5 w-5" aria-hidden="true" /></span>
      <p className="eyebrow mt-5">Invitation</p>
      <h1 className="mt-2 text-xl font-semibold tracking-tight text-ink">
        {hasAccess ? `You already have ${invite.repository}` : `Join ${invite.repository}`}
      </h1>
      <dl className="mt-4 space-y-2 text-[13.5px]">
        {invite.full_name && (
          <div className="flex gap-2"><dt className="w-24 shrink-0 text-ink-3">Repository</dt><dd className="min-w-0 truncate font-mono text-ink">{invite.full_name}</dd></div>
        )}
        <div className="flex gap-2"><dt className="w-24 shrink-0 text-ink-3">Shared by</dt><dd className="min-w-0 truncate text-ink">{invite.owner_label || 'A teammate'}</dd></div>
        {/* Someone who already has access sees their role, not the link's (they differed after a role change). */}
        <div className="flex items-center gap-2">
          <dt className="w-24 shrink-0 text-ink-3">{hasAccess ? 'Your access' : 'Access'}</dt>
          <dd>
            {invite.status === 'owner'
              ? <Badge tone="accent">Owner</Badge>
              : <Badge tone={shownRole === 'editor' ? 'accent' : 'neutral'}>{ROLE_LABELS[shownRole]}</Badge>}
          </dd>
        </div>
      </dl>
      {hasAccess ? (
        <p className="mt-4 text-sm leading-relaxed text-ink-2">
          {invite.status === 'owner'
            ? 'You own this repository, so there is nothing to join.'
            : `You joined earlier as ${invite.access_role === 'editor' ? 'an editor' : 'a viewer'}. Your role stays as the owner set it.`}
        </p>
      ) : (
        <p className="mt-4 text-sm leading-relaxed text-ink-2">
          {ROLE_DESCRIPTIONS[invite.role]} You ask questions using {invite.owner_label ? `${invite.owner_label}’s` : 'their'} index, so nothing is indexed again and nothing counts toward your storage. Your conversations stay private to you.
        </p>
      )}
      {!hasAccess && invite.role_kept && (
        <p className="mt-3 text-[13px] leading-relaxed text-ink-3">
          You were a member before, and {invite.owner_label || 'the owner'} changed your role after this link was made, so you rejoin as {invite.role === 'editor' ? 'an editor' : 'a viewer'}.
        </p>
      )}
      {joinError && <p className="mt-4 rounded-xl bg-danger-soft px-3 py-2.5 text-[13px] text-danger" role="alert">{joinError}</p>}
      <div className="mt-6 flex flex-col gap-2 sm:flex-row">
        {hasAccess ? (
          <Button variant="primary" size="md" iconRight={ArrowRight} to={repoPath(invite.repo_name)} className="w-full sm:w-auto">Open {invite.repo_name}</Button>
        ) : (
          <Button variant="primary" size="md" loading={joining} onClick={join} className="w-full sm:w-auto">Join</Button>
        )}
        <Button variant="ghost" size="md" to="/app" className="w-full sm:w-auto">Not now</Button>
      </div>
      {!hasAccess && invite.expires_at && (
        <p className="mt-4 text-[12px] text-ink-3">This link works until {formatDate(invite.expires_at)}.</p>
      )}
    </Shell>
  );
};

export default InvitePage;
