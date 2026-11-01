import { ExternalLink, FileCode2, GitCommitHorizontal, Link2Off, MessageSquareText } from 'lucide-react';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { useParams } from 'react-router-dom';
import remarkGfm from 'remark-gfm';
import { errorMessage, getSharedAnswer } from '../api/client';
import Button from '../components/ui/Button';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { shortSha } from '../lib/format';
import { citationLabel, detailUnder, formatDate } from '../lib/sharing';

const UNAVAILABLE = 'This shared answer isn’t available';

const REMARK_PLUGINS = [remarkGfm];

/** Links in a shared answer open in a new tab; images stay links (no remote loads). */
const MARKDOWN_COMPONENTS = {
  a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>,
  img: ({ src, alt }) => <a href={src} target="_blank" rel="noreferrer noopener">{alt || src}</a>,
};

const Shell = ({ children }) => (
  <main id="main" className="content-shell py-12 sm:py-16">
    <div className="mx-auto w-full max-w-3xl">{children}</div>
  </main>
);

/**
 * /shared/answer/<token>: a read-only copy of one answer (question, answer,
 * citations pinned to the indexed commit), for anyone signed in with the link.
 */
const SharedAnswerPage = () => {
  const { token = '' } = useParams();
  const [answer, setAnswer] = useState(null);
  const [problem, setProblem] = useState('');
  useDocumentTitle(answer ? `Shared answer · ${answer.repository || 'Codebase Intel'}` : 'Shared answer');

  useEffect(() => {
    let active = true;
    setAnswer(null);
    setProblem('');
    getSharedAnswer(token)
      .then((response) => { if (active) setAnswer(response.data); })
      .catch((error) => {
        if (active) setProblem(detailUnder(UNAVAILABLE, errorMessage(error, 'It could not be loaded. Try again.'), 'Ask the person who shared it for a new link.'));
      });
    return () => { active = false; };
  }, [token]);

  if (problem) {
    return (
      <Shell>
        <div className="mx-auto max-w-md rounded-3xl border border-line bg-surface p-6 sm:p-8" role="alert">
          <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2 text-ink-3"><Link2Off className="h-5 w-5" aria-hidden="true" /></span>
          <h1 className="mt-4 text-lg font-semibold text-ink">{UNAVAILABLE}</h1>
          <p className="mt-2 text-sm leading-relaxed text-ink-2">{problem}</p>
          <Button className="mt-6" variant="secondary" size="sm" to="/app">Go to your workspace</Button>
        </div>
      </Shell>
    );
  }
  if (!answer) {
    return (
      <Shell>
        <div role="status" aria-label="Loading the shared answer" className="space-y-3">
          <div className="skeleton h-4 w-40" /><div className="skeleton h-7 w-3/4" /><div className="skeleton h-40 w-full rounded-2xl" />
        </div>
      </Shell>
    );
  }

  const citations = Array.isArray(answer.citations) ? answer.citations : [];
  return (
    <Shell>
      <p className="eyebrow">Shared answer</p>
      <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-ink-3">
        {answer.repository && (
          <a href={`https://github.com/${answer.repository}`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-mono text-ink-2 hover:text-ink">
            {answer.repository} <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>
        )}
        {answer.commit_sha && (
          <span className="inline-flex items-center gap-1 font-mono" title={`Indexed commit ${answer.commit_sha}`}>
            <GitCommitHorizontal className="h-3.5 w-3.5" aria-hidden="true" /> {shortSha(answer.commit_sha)}
          </span>
        )}
        <span>Shared by {answer.yours ? 'you' : answer.shared_by}{answer.shared_at ? ` on ${formatDate(answer.shared_at)}` : ''}</span>
      </p>
      {answer.question && (
        <div className="mt-6 flex items-start gap-3 rounded-2xl border border-line bg-surface-2/60 px-4 py-3">
          <MessageSquareText className="mt-0.5 h-4 w-4 shrink-0 text-ink-3" aria-hidden="true" />
          <h1 className="text-[15px] font-semibold leading-relaxed text-ink">{answer.question}</h1>
        </div>
      )}
      <article className="prose-answer mt-6" aria-label="Answer">
        <ReactMarkdown remarkPlugins={REMARK_PLUGINS} skipHtml components={MARKDOWN_COMPONENTS}>{answer.answer}</ReactMarkdown>
      </article>
      {citations.length > 0 && (
        <section aria-labelledby="shared-sources-heading" className="mt-8">
          <h2 id="shared-sources-heading" className="text-[12px] font-semibold uppercase tracking-wider text-ink-3">Sources ({citations.length})</h2>
          <ul className="mt-2 divide-y divide-line rounded-2xl border border-line bg-surface">
            {citations.map((citation) => (
              <li key={`${citation.file_path}:${citation.start_line}`} className="flex items-center gap-2.5 px-4 py-2.5 text-[13px]">
                <FileCode2 className="h-4 w-4 shrink-0 text-ink-3" aria-hidden="true" />
                {citation.github_url ? (
                  <a href={citation.github_url} target="_blank" rel="noreferrer" className="min-w-0 truncate font-mono text-ink hover:text-accent-strong">
                    {citationLabel(citation)}
                  </a>
                ) : <span className="min-w-0 truncate font-mono text-ink">{citationLabel(citation)}</span>}
                {citation.github_url && <ExternalLink className="h-3 w-3 shrink-0 text-ink-3" aria-hidden="true" />}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[12px] text-ink-3">Links open the exact lines on GitHub at the commit the answer was written from.</p>
        </section>
      )}
      <p className="mt-8 text-[12px] text-ink-3">
        A read-only copy{answer.answered_at ? ` of an answer from ${formatDate(answer.answered_at)}` : ''}. It does not change when the conversation or the index does.
      </p>
    </Shell>
  );
};

export default SharedAnswerPage;
