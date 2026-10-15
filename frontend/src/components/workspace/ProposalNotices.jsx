import { AlertTriangle, Info } from 'lucide-react';
import { approximatePaths, incompleteness } from '../../lib/proposal';

/**
 * Warnings the server attached to a proposal: files it planned to change but
 * did not (`unchanged_files` / `incomplete`) and files whose content was
 * reconstructed from the index (`approximate_files`).
 */
const ProposalNotices = ({ proposal, refreshedPaths = new Set(), className = '' }) => {
  const { unchanged, incomplete } = incompleteness(proposal);
  const approximate = [...approximatePaths(proposal)].filter((path) => !refreshedPaths.has(path));
  if (!incomplete && !approximate.length) return null;
  return (
    <div className={`space-y-2 ${className}`}>
      {incomplete && (
        <div className="rounded-xl border border-warning/40 bg-warning-soft px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2" role="note">
          <p className="flex items-center gap-2 font-semibold text-ink">
            <AlertTriangle className="h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
            This change may be incomplete
          </p>
          {unchanged.length > 0 ? (
            <>
              <p className="mt-1">These files were part of the plan but were not changed:</p>
              <ul className="mt-1 space-y-0.5">
                {unchanged.map((item) => (
                  <li key={item.file_path} className="break-words">
                    <span className="font-mono text-[12px] text-ink">{item.file_path}</span>
                    {item.reason ? <span> — {item.reason}</span> : null}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="mt-1">Some planned changes could not be written. Review carefully or ask again with a narrower request.</p>
          )}
        </div>
      )}
      {approximate.length > 0 && (
        <div className="rounded-xl border border-info/30 bg-info-soft px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2" role="note">
          <p className="flex items-center gap-2 font-semibold text-ink">
            <Info className="h-4 w-4 shrink-0 text-info" aria-hidden="true" />
            {approximate.length === 1 ? '1 file was' : `${approximate.length} files were`} reconstructed from the index
          </p>
          <p className="mt-1">Refresh from GitHub before pushing so the change is applied to the real file:</p>
          <ul className="mt-1 space-y-0.5">
            {approximate.map((path) => <li key={path} className="break-words font-mono text-[12px] text-ink">{path}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
};

export default ProposalNotices;
