import { Check, Copy, Link2 } from 'lucide-react';
import { useState } from 'react';
import { errorMessage, revokeAnswerShare, shareAnswer } from '../../api/client';
import { toast } from '../../hooks/useToasts';
import { canShareAnswer, linkUrl } from '../../lib/sharing';
import Button from '../ui/Button';
import Modal from '../ui/Modal';

/**
 * "Share answer" under a finished reply: makes a read-only link to a copy of
 * the question, the answer and its citations. The link is shown once; it can
 * be revoked here or later from the account page.
 */
const ShareAnswerButton = ({ message, repo }) => {
  const [busy, setBusy] = useState(false);
  const [share, setShare] = useState(null);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [revoking, setRevoking] = useState(false);
  if (!repo || !canShareAnswer(message)) return null;

  const create = async () => {
    setBusy(true);
    setError('');
    try {
      const response = await shareAnswer(repo.repo_name, message.id, message.content);
      setShare({ ...response.data, url: linkUrl(response.data.path, window.location.origin) });
    } catch (shareError) {
      toast.error('The answer could not be shared', { description: errorMessage(shareError, 'Try again in a moment.') });
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(share.url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError('Copy failed. Select the link and copy it yourself.');
    }
  };

  const revoke = async () => {
    setRevoking(true);
    setError('');
    try {
      await revokeAnswerShare(share.id);
      setShare(null);
      toast('Stopped sharing the answer', { description: 'Its link no longer opens.' });
    } catch (revokeError) {
      setError(errorMessage(revokeError, 'Sharing could not be stopped. Try again.'));
    } finally {
      setRevoking(false);
    }
  };

  return (
    <>
      <Button size="xs" variant="ghost" icon={Link2} loading={busy} onClick={create} className="mt-2 text-ink-3" data-slot="share-answer">
        Share answer
      </Button>
      <Modal
        open={Boolean(share)}
        onClose={() => setShare(null)}
        busy={revoking}
        size="sm"
        title="Share this answer"
        description="Anyone signed in with the link sees a read-only copy of the question, this answer and its sources. Later changes to your conversation do not change it."
        footer={(
          <>
            <Button key="stop" variant="danger-ghost" size="sm" className="mr-auto" loading={revoking} onClick={revoke}>Stop sharing</Button>
            <Button key="done" variant="secondary" size="sm" onClick={() => setShare(null)}>Done</Button>
          </>
        )}
      >
        {share && (
          <div className="space-y-2">
            <label htmlFor={`answer-link-${share.id}`} className="text-[12.5px] font-medium text-ink-2">Answer link</label>
            <div className="flex gap-2">
              <input
                id={`answer-link-${share.id}`}
                readOnly
                value={share.url}
                onFocus={(event) => event.target.select()}
                className="h-9 min-w-0 flex-1 rounded-lg border border-line bg-surface px-2.5 font-mono text-[12px] text-ink outline-none focus:border-accent"
              />
              <Button size="sm" variant="primary" icon={copied ? Check : Copy} onClick={copy}>{copied ? 'Copied' : 'Copy link'}</Button>
            </div>
            <p className="text-[12.5px] leading-relaxed text-ink-3">The link is shown only now. You can stop sharing later from your account page.</p>
            {error && <p className="text-[12.5px] text-danger" role="alert">{error}</p>}
          </div>
        )}
      </Modal>
    </>
  );
};

export default ShareAnswerButton;
