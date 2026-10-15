import { Check, ChevronDown, Copy } from 'lucide-react';
import { memo, useMemo, useState } from 'react';
import { highlight } from '../lib/prism';

// Long snippets start folded so one block cannot push the rest of the
// answer (and its sources) far below the fold.
const FOLD_ABOVE = 28;
const FOLDED_LINES = 18;

const LANGUAGE_NAMES = {
  ts: 'TypeScript', typescript: 'TypeScript', tsx: 'TSX', js: 'JavaScript', javascript: 'JavaScript', jsx: 'JSX',
  py: 'Python', python: 'Python', sh: 'Shell', bash: 'Shell', shell: 'Shell', zsh: 'Shell', json: 'JSON', yaml: 'YAML',
  yml: 'YAML', sql: 'SQL', prisma: 'Prisma', go: 'Go', rs: 'Rust', rust: 'Rust', rb: 'Ruby', ruby: 'Ruby', java: 'Java',
  kt: 'Kotlin', css: 'CSS', scss: 'SCSS', html: 'HTML', md: 'Markdown', markdown: 'Markdown', diff: 'Diff', graphql: 'GraphQL',
  toml: 'TOML', dockerfile: 'Dockerfile', php: 'PHP', cs: 'C#', csharp: 'C#', cpp: 'C++', c: 'C', swift: 'Swift',
};

/** Fenced code inside answers: highlighted, copyable, horizontally scrollable. */
const CodeBlock = ({ code, language, title }) => {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const html = useMemo(() => highlight(code, language), [code, language]);
  const lineCount = useMemo(() => String(code || '').split('\n').length, [code]);
  const foldable = lineCount > FOLD_ABOVE;
  const folded = foldable && !expanded;
  const name = title || LANGUAGE_NAMES[String(language || '').toLowerCase()] || language || 'Code';

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be denied; the code remains selectable.
    }
  };
  return (
    <div className="group my-3 overflow-hidden rounded-xl border border-code-line bg-code">
      <div className="flex items-center justify-between gap-2 border-b border-code-line bg-code-2 px-3 py-1.5">
        <span className="truncate font-mono text-[11px] text-code-muted">
          {name}
          {lineCount > 1 && <span className="opacity-70"> · {lineCount} lines</span>}
        </span>
        <button
          type="button"
          onClick={copy}
          className="flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] text-code-muted transition-colors hover:bg-code-line hover:text-code-ink focus-visible:text-code-ink"
          aria-label={copied ? 'Code copied' : 'Copy code'}
        >
          {copied ? <Check className="h-3 w-3" aria-hidden="true" /> : <Copy className="h-3 w-3" aria-hidden="true" />}
          <span aria-live="polite">{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>
      <div className="relative">
        <pre
          tabIndex={0}
          aria-label={`${name} snippet`}
          className="code-scroll code-tokens m-0 overflow-x-auto p-3.5 font-mono text-[12.5px] leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
          style={folded ? { maxHeight: `calc(${FOLDED_LINES} * 1.625em + 1.75rem)`, overflowY: 'hidden' } : undefined}
        >
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>
        {folded && <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-code to-transparent" aria-hidden="true" />}
      </div>
      {foldable && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          className="flex w-full items-center justify-center gap-1.5 border-t border-code-line bg-code-2 px-3 py-1.5 text-[11.5px] font-medium text-code-muted transition-colors hover:text-code-ink focus-visible:text-code-ink"
        >
          <ChevronDown className={`h-3.5 w-3.5 transition-transform ${expanded ? 'rotate-180' : ''}`} aria-hidden="true" />
          {expanded ? 'Show less' : `Show all ${lineCount} lines`}
        </button>
      )}
    </div>
  );
};

export default memo(CodeBlock);
