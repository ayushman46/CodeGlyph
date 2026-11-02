import { useEffect, useState } from 'react';
import { errorMessage, listAnswerShares, revokeAnswerShare } from '../../api/client';
import { toast } from '../../hooks/useToasts';
import { formatDate } from '../../lib/sharing';
import Button from '../ui/Button';

/** Your active shared-answer links, each with Stop sharing (account page). */
const SharedAnswersList = () => {
  const [shares, setShares] = useState(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState('');

  useEffect(() => {
    let active = true;
    listAnswerShares()
      .then((response) => { if (active) setShares(response.data.shares || []); })
      .catch((loadError) => { if (active) setError(errorMessage(loadError, 'Your shared answers could not be loaded.')); });
    return () => { active = false; };
  }, []);

  const revoke = async (share) => {
    setPending(share.id);
    try {
      await revokeAnswerShare(share.id);
      setShares((current) => current.filter((item) => item.id !== share.id));
      toast('Stopped sharing the answer', { description: 'Its link no longer opens.' });
    } catch (revokeError) {
      toast.error('Sharing could not be stopped', { description: errorMessage(revokeError, 'Try again.') });
    } finally {
      setPending('');
    }
  };

  return (
    <section aria-labelledby="team-answers-heading" className="mt-6 min-w-0">
      <h3 id="team-answers-heading" className="text-[12px] font-semibold uppercase tracking-wider text-ink-3">Answers you shared</h3>
      {error ? (
        <p className="mt-2 text-[13px] text-danger" role="alert">{error}</p>
      ) : shares === null ? (
        <div className="skeleton mt-2 h-10 w-full" role="status" aria-label="Loading shared answers" />
      ) : shares.length === 0 ? (
        <p className="mt-2 text-[13px] leading-relaxed text-ink-2">Use Share answer under any answer to send a read-only copy with its sources.</p>
      ) : (
        <ul className="mt-2 divide-y divide-line overflow-hidden rounded-2xl border border-line" aria-label="Answers you shared">
          {shares.map((share) => (
            <li key={share.id} className="flex items-center gap-3 px-4 py-2.5">
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm text-ink">{share.question || 'Shared answer'}</span>
                <span className="block truncate text-xs text-ink-2">{[share.repository, formatDate(share.created_at)].filter(Boolean).join(' · ')}</span>
              </span>
              <Button size="xs" variant="danger-ghost" loading={pending === share.id} onClick={() => revoke(share)} aria-label={`Stop sharing “${share.question || 'answer'}”`}>
                Stop sharing
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
};

export default SharedAnswersList;
