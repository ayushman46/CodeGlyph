import { Check, Copy, Link2, RefreshCw, UserMinus } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import {
  changeMemberRole, createInvite, errorMessage, getSharing, removeMember, revokeInvite,
} from '../../api/client';
import { toast } from '../../hooks/useToasts';
import {
  EXPIRY_OPTIONS, MEMBER_ROLES, ROLE_DESCRIPTIONS, ROLE_LABELS, expiryLabel, formatDate, inviteSentence, inviteUrl,
} from '../../lib/sharing';
import useStore from '../../store/useStore';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import Segmented from '../ui/Segmented';
import Spinner from '../ui/Spinner';

const selectClass = 'h-8 rounded-lg border border-line bg-surface px-2 text-[13px] text-ink outline-none focus:border-accent disabled:opacity-50';

const SectionTitle = ({ id, children, aside }) => (
  <div className="flex items-center justify-between gap-2">
    <h3 id={id} className="text-[12px] font-semibold uppercase tracking-wider text-ink-3">{children}</h3>
    {aside}
  </div>
);

/** The link that was just created: shown once, with a copy button. */
const CreatedLink = ({ created }) => {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(created.url);
      setCopied(true);
      toast.success('Invite link copied');
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error('Copy failed', { description: 'Select the link and copy it yourself.' });
    }
  };
  return (
    <div className="mt-3 rounded-xl border border-accent/30 bg-accent-soft/40 p-3" data-testid="created-invite">
      <label htmlFor="invite-link" className="text-[12.5px] font-medium text-ink-2">New invite link</label>
      <div className="mt-1.5 flex gap-2">
        <input
          id="invite-link"
          readOnly
          value={created.url}
          onFocus={(event) => event.target.select()}
          className="h-9 min-w-0 flex-1 rounded-lg border border-line bg-surface px-2.5 font-mono text-[12px] text-ink outline-none focus:border-accent"
        />
        <Button size="sm" variant="primary" icon={copied ? Check : Copy} onClick={copy}>{copied ? 'Copied' : 'Copy link'}</Button>
      </div>
      <p className="mt-2 text-[12.5px] leading-relaxed text-ink-2">
        {inviteSentence(created.invite.role, created.invite.expires_at)} Copy it now: for security it is shown only once.
      </p>
    </div>
  );
};

/**
 * Owner's sharing controls for one repository: create and copy invite links
 * (role + expiry), see and revoke active links, and change or remove members.
 */
const ShareDialog = ({ repo, open, onClose }) => {
  const fetchRepos = useStore((state) => state.fetchRepos);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [role, setRole] = useState('viewer');
  const [days, setDays] = useState(7);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState(null);
  const [pending, setPending] = useState('');
  const [confirmRemove, setConfirmRemove] = useState('');
  const repoName = repo?.repo_name;

  const load = useCallback(async () => {
    if (!repoName) return;
    setLoading(true);
    setError('');
    try {
      const response = await getSharing(repoName);
      setData(response.data);
    } catch (loadError) {
      setError(errorMessage(loadError, 'Who this repository is shared with could not be loaded.'));
    } finally {
      setLoading(false);
    }
  }, [repoName]);

  useEffect(() => {
    if (!open) return;
    setCreated(null);
    setConfirmRemove('');
    void load();
  }, [open, load]);

  const run = async (key, action, fallback) => {
    setPending(key);
    setError('');
    try {
      await action();
      return true;
    } catch (actionError) {
      setError(errorMessage(actionError, fallback));
      return false;
    } finally {
      setPending('');
    }
  };

  const create = async () => {
    setCreating(true);
    setError('');
    try {
      const response = await createInvite(repoName, role, days);
      const { invite, path } = response.data;
      setCreated({ url: inviteUrl(path, window.location.origin), invite });
      setData((current) => (current ? { ...current, invites: [invite, ...current.invites] } : current));
    } catch (createError) {
      setError(errorMessage(createError, 'The invite link could not be created.'));
    } finally {
      setCreating(false);
    }
  };

  const revoke = (invite) => run(`invite:${invite.id}`, async () => {
    await revokeInvite(repoName, invite.id);
    setData((current) => ({ ...current, invites: current.invites.filter((item) => item.id !== invite.id) }));
    if (created?.invite.id === invite.id) setCreated(null);
    toast('Invite link revoked', { description: 'It no longer works. People who already joined keep their access.' });
  }, 'The link could not be revoked.');

  const changeRole = (member, nextRole) => run(`member:${member.user_id}`, async () => {
    await changeMemberRole(repoName, member.user_id, nextRole);
    setData((current) => ({
      ...current,
      members: current.members.map((item) => (item.user_id === member.user_id ? { ...item, role: nextRole } : item)),
    }));
    toast.success(`${member.label} is now ${nextRole === 'editor' ? 'an editor' : 'a viewer'}`);
  }, 'The role could not be changed.');

  const remove = (member) => run(`member:${member.user_id}`, async () => {
    await removeMember(repoName, member.user_id);
    setConfirmRemove('');
    setData((current) => ({ ...current, members: current.members.filter((item) => item.user_id !== member.user_id) }));
    toast(`${member.label} was removed`, { description: `They can no longer open ${repoName}.` });
    void fetchRepos();
  }, 'The member could not be removed.');

  const members = data?.members || [];
  const invites = data?.invites || [];

  return (
    <Modal
      open={open}
      onClose={onClose}
      busy={creating}
      size="md"
      title={`Share ${repoName}`}
      description="Teammates you invite ask questions using your index, so nobody indexes it again. It counts only toward your storage."
      footer={(
        <>
          <p className="mr-auto text-[12px] leading-snug text-ink-3">Only you can rename, re-index, delete or share this repository.</p>
          <Button variant="secondary" size="sm" onClick={onClose}>Done</Button>
        </>
      )}
    >
      <div className="space-y-6">
        <section aria-labelledby="share-invite-heading">
          <SectionTitle id="share-invite-heading">Invite with a link</SectionTitle>
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <div>
              <span className="block text-[12.5px] font-medium text-ink-2" id="invite-role-label">Role</span>
              <Segmented
                className="mt-1.5"
                label="Role for the invite link"
                value={role}
                onChange={setRole}
                options={MEMBER_ROLES.map((value) => ({ value, label: ROLE_LABELS[value] }))}
              />
            </div>
            <div>
              <label htmlFor="invite-expiry" className="block text-[12.5px] font-medium text-ink-2">Link works for</label>
              <select id="invite-expiry" value={days} onChange={(event) => setDays(Number(event.target.value))} className={`mt-1.5 ${selectClass}`}>
                {EXPIRY_OPTIONS.map((option) => <option key={option.days} value={option.days}>{option.label}</option>)}
              </select>
            </div>
            <Button variant="primary" size="sm" icon={Link2} loading={creating} onClick={create} className="ml-auto">Create link</Button>
          </div>
          <p className="mt-2 text-[12.5px] leading-relaxed text-ink-3">{ROLE_LABELS[role]}s: {ROLE_DESCRIPTIONS[role].charAt(0).toLowerCase()}{ROLE_DESCRIPTIONS[role].slice(1)}</p>
          {created && <CreatedLink key={created.invite.id} created={created} />}
        </section>

        {error && <p className="rounded-xl bg-danger-soft px-3 py-2.5 text-[13px] text-danger" role="alert">{error}</p>}

        <section aria-labelledby="share-links-heading">
          <SectionTitle
            id="share-links-heading"
            aside={loading ? <Spinner className="h-3.5 w-3.5 text-ink-3" /> : (
              <Button size="icon-sm" variant="ghost" icon={RefreshCw} aria-label="Refresh sharing" title="Refresh" onClick={load} />
            )}
          >
            Active invite links{invites.length ? ` (${invites.length})` : ''}
          </SectionTitle>
          {!data && loading ? (
            <div className="mt-3 space-y-2" aria-hidden="true"><div className="skeleton h-10" /><div className="skeleton h-10" /></div>
          ) : invites.length === 0 ? (
            <p className="mt-2 text-[13px] text-ink-3">No active links. Links stop working when they expire or you revoke them.</p>
          ) : (
            <ul className="mt-2 divide-y divide-line rounded-xl border border-line" aria-label="Active invite links">
              {invites.map((invite) => (
                <li key={invite.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 text-[13px]">
                  <Badge tone={invite.role === 'editor' ? 'accent' : 'neutral'}>{ROLE_LABELS[invite.role]}</Badge>
                  <span className="font-mono text-[12px] text-ink-2" title="The end of the link, to tell links apart">…{invite.token_hint}</span>
                  <span className="text-ink-3">{expiryLabel(invite.expires_at)}</span>
                  <span className="text-ink-3">· {invite.accepted_count} joined</span>
                  <Button
                    size="xs"
                    variant="danger-ghost"
                    className="ml-auto"
                    loading={pending === `invite:${invite.id}`}
                    onClick={() => revoke(invite)}
                    aria-label={`Revoke the ${ROLE_LABELS[invite.role].toLowerCase()} link ending ${invite.token_hint}`}
                  >
                    Revoke
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-labelledby="share-members-heading">
          <SectionTitle id="share-members-heading">People with access</SectionTitle>
          <ul className="mt-2 divide-y divide-line rounded-xl border border-line" aria-label="People with access">
            <li className="flex items-center gap-3 px-3 py-2.5 text-[13px]">
              <span className="min-w-0 flex-1 truncate font-medium text-ink">You</span>
              <Badge tone="success">Owner</Badge>
            </li>
            {members.map((member) => (
              <li key={member.user_id} className="px-3 py-2.5 text-[13px]" data-testid="member-row">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-ink">{member.label}</span>
                    <span className="block text-[12px] text-ink-3">Joined {formatDate(member.joined_at)}</span>
                  </span>
                  <select
                    aria-label={`Role for ${member.label}`}
                    value={member.role}
                    disabled={pending === `member:${member.user_id}`}
                    onChange={(event) => changeRole(member, event.target.value)}
                    className={selectClass}
                  >
                    {MEMBER_ROLES.map((value) => <option key={value} value={value}>{ROLE_LABELS[value]}</option>)}
                  </select>
                  <Button
                    size="xs"
                    variant="danger-ghost"
                    icon={UserMinus}
                    onClick={() => setConfirmRemove(member.user_id)}
                    aria-label={`Remove ${member.label}`}
                  >
                    Remove
                  </Button>
                </div>
                {confirmRemove === member.user_id && (
                  <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg bg-danger-soft px-3 py-2 text-[12.5px] text-danger" role="group" aria-label={`Confirm removing ${member.label}`}>
                    <span className="mr-auto">Remove {member.label}? They lose access right away.</span>
                    <Button size="xs" variant="ghost" onClick={() => setConfirmRemove('')}>Cancel</Button>
                    <Button size="xs" variant="danger" loading={pending === `member:${member.user_id}`} onClick={() => remove(member)}>Remove</Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
          {data && members.length === 0 && (
            <p className="mt-2 text-[13px] text-ink-3">Nobody else has access yet. Create a link above and send it to a teammate.</p>
          )}
        </section>
      </div>
    </Modal>
  );
};

export default ShareDialog;
