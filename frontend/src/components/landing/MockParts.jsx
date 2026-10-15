import { FileCode2 } from 'lucide-react';
import { highlight } from './highlight';

/* Small presentational pieces shared by the landing page product mockups.
 * Everything here is decorative: the mockups that use them are aria-hidden
 * and described for assistive technology by a figcaption instead. */

export const WindowDots = ({ className = '' }) => (
  <div className={`flex shrink-0 items-center gap-1.5 ${className}`}>
    <span className="h-2.5 w-2.5 rounded-full bg-danger/70" />
    <span className="h-2.5 w-2.5 rounded-full bg-warning/70" />
    <span className="h-2.5 w-2.5 rounded-full bg-success/70" />
  </div>
);

/* The Codebase Intel mark without the gradient <defs>, so it can repeat on the
 * page without duplicating the SVG id used by the header logo. */
export const BrandGlyph = ({ className = 'h-6 w-6' }) => (
  <svg className={className} viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <rect width="32" height="32" rx="9" className="fill-accent" />
    <path d="M12.5 10.5 7.5 16l5 5.5M19.5 10.5l5 5.5-5 5.5" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
    <circle cx="16" cy="16" r="1.9" fill="#fff" />
  </svg>
);

/* Citation pill in the product's path:L12-L40 format. The folder prefix only
 * appears from the `dirFrom` breakpoint up, where the column is wide enough to
 * keep the whole path on one line; below it the chip shows just the file. If a
 * path ever does wrap, <wbr> makes it break after a slash, not mid-name. */
const DIR_VISIBILITY = { sm: 'hidden sm:inline', xl: 'hidden xl:inline' };

const withSlashBreaks = (path) => path.split('/').map((part, index, parts) => (
  index < parts.length - 1 ? <span key={index}>{part}/<wbr /></span> : part
));

export const CitationChip = ({ dir = '', dirFrom = 'sm', file, lines, className = '' }) => (
  <span className={`inline-flex max-w-full items-center gap-1 rounded-md border border-accent/25 bg-accent-soft px-1.5 py-px align-middle font-mono text-[0.78em] font-medium leading-5 text-accent-strong ${className}`}>
    <FileCode2 className="h-3 w-3 shrink-0" aria-hidden="true" />
    <span className="min-w-0 wrap-anywhere">
      {dir && <span className={`font-normal ${DIR_VISIBILITY[dirFrom] || DIR_VISIBILITY.sm}`}>{withSlashBreaks(dir)}</span>}
      {file}:{lines}
    </span>
  </span>
);

export const DIFF_MARKERS = { add: '+', del: '−', ctx: ' ' };

export const diffRowClass = (type) => (type === 'add' ? 'bg-diff-add' : type === 'del' ? 'bg-diff-del' : '');

export const diffMarkerClass = (type) => (type === 'add' ? 'text-diff-add-ink' : type === 'del' ? 'text-diff-del-ink' : 'text-code-muted');

/* Compact single-gutter diff used by the smaller mockups. */
export const MiniDiff = ({ lines, size = 'md', className = '' }) => (
  <div className={`code-tokens py-1.5 font-mono leading-[1.75] ${size === 'sm' ? 'text-[10px]' : 'text-[10.5px]'} ${className}`}>
    {lines.map((line, index) => (
      <div key={index} className={`flex whitespace-pre ${diffRowClass(line.type)}`}>
        <span className={`w-4 shrink-0 select-none text-center ${diffMarkerClass(line.type)}`}>{DIFF_MARKERS[line.type]}</span>
        <span className="min-w-0 overflow-hidden pr-1.5">{highlight(line.code)}</span>
      </div>
    ))}
  </div>
);

/* Additions and deletions, for use on the dark code surfaces only (the diff
 * ink colours are tuned for bg-code). */
export const DiffStat = ({ added, removed = 0, className = '' }) => (
  <span className={`shrink-0 font-mono tabular-nums ${className}`}>
    <span className="text-diff-add-ink">+{added}</span>
    {removed > 0 && (
      <>
        {' '}
        <span className="text-diff-del-ink">{'−'}{removed}</span>
      </>
    )}
  </span>
);
