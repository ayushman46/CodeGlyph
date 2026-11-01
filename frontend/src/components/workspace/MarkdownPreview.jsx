import { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { resolveRepositoryLink } from '../../lib/codeNav';

const REMARK_PLUGINS = [remarkGfm];

/**
 * A Markdown file rendered for reading. Raw HTML is skipped and images are
 * shown as links (nothing is fetched from other sites); links to other
 * files of the repository open them in the viewer.
 */
const MarkdownPreview = ({ content, path, onOpenPath }) => {
  const components = useMemo(() => ({
    a: ({ href, children }) => {
      const target = resolveRepositoryLink(href, path);
      if (target && onOpenPath) {
        return (
          <button type="button" onClick={() => onOpenPath(target)} className="font-medium text-accent-strong underline underline-offset-2" title={`Open ${target}`}>
            {children}
          </button>
        );
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
    },
    img: ({ src, alt }) => (src ? <a href={src} target="_blank" rel="noreferrer">{alt || 'image'}</a> : null),
    pre: ({ children }) => <pre className="my-3 overflow-x-auto rounded-lg bg-code p-3 font-mono text-[12.5px] leading-5 text-code-ink">{children}</pre>,
    code: ({ className, children }) => (className
      ? <code className={className}>{children}</code>
      : <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[0.88em]">{children}</code>),
    table: ({ children }) => <div className="answer-table"><table>{children}</table></div>,
  }), [path, onOpenPath]);

  return (
    <div className="prose-answer px-5 py-4 text-[14px] text-ink sm:px-7">
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} skipHtml components={components}>{content}</ReactMarkdown>
    </div>
  );
};

export default MarkdownPreview;
