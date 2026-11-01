import { ArrowRight, BookOpen, Check, Compass, Copy, FileCode2, Folder } from 'lucide-react';
import { useState } from 'react';
import { announce } from '../../hooks/useToasts';
import { formatCount } from '../../lib/format';
import useStore from '../../store/useStore';

/** "package.json:L94": the file and line a fact comes from; opens the viewer there. */
const Cite = ({ source, onOpen }) => {
  if (!source?.path) return null;
  return (
    <button
      type="button"
      onClick={() => onOpen(source)}
      className="shrink-0 rounded-md border border-line bg-surface-2/60 px-1.5 py-px font-mono text-[11px] text-ink-3 transition-colors hover:border-accent/40 hover:text-accent-strong"
      aria-label={`Open ${source.path} at line ${source.line}`}
      title={`Open ${source.path} at line ${source.line}`}
    >
      {source.path}:L{source.line}
    </button>
  );
};

const CopyCommand = ({ command }) => {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      announce('Command copied');
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be denied; the command stays selectable.
    }
  };
  return (
    <button type="button" onClick={copy} className="rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink" aria-label={`Copy ${command}`} title="Copy">
      {copied ? <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
    </button>
  );
};

const Fact = ({ icon: Icon, label, children, source, onOpen }) => (
  <div className="flex items-start gap-2.5">
    <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />
    <div className="min-w-0 flex-1">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-[12px] text-ink-3">{label}</span>
        <Cite source={source} onOpen={onOpen} />
      </div>
      {children}
    </div>
  </div>
);

/**
 * "Start here": the entry file, the core folder, cited install / test / run
 * commands and a reading path for someone new to the repository. Built by
 * the overview endpoint from indexed files only (no model call); every fact
 * opens the file viewer at the line it came from.
 */
const StartHere = ({ data, loading = false }) => {
  const openFile = useStore((state) => state.openFile);
  if (loading) {
    return <div className="skeleton h-56 rounded-2xl" aria-hidden="true" />;
  }
  if (!data) return null;
  const open = ({ path, line }) => openFile({ path, start: line, end: line });
  const { entry, core, summary } = data;
  const commands = data.commands || [];
  const steps = data.steps || [];
  const guides = data.guides || [];
  const main = entry?.main || null;

  return (
    // A container: the Overview narrows when the file viewer opens beside it.
    <section className="@container rounded-2xl border border-line bg-surface p-4 sm:p-5" aria-labelledby="start-here-title" data-slot="start-here">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id="start-here-title" className="flex items-center gap-1.5 text-[14px] font-semibold text-ink">
          <Compass className="h-4 w-4 text-accent" aria-hidden="true" /> Start here
        </h2>
        <span className="text-[11.5px] text-ink-3">From the indexed files · every fact links to its line</span>
      </div>
      {summary?.text && (
        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] leading-relaxed text-ink-2">
          <span>{summary.text}</span> <Cite source={summary.source} onOpen={open} />
        </p>
      )}

      <div className="mt-4 grid grid-cols-1 gap-5 @2xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="space-y-4">
          {(entry || core) && (
            <div className="space-y-3">
              {entry && (
                <Fact icon={FileCode2} label="Entry point" source={entry.source} onOpen={open}>
                  <button type="button" onClick={() => open(entry)} className="mt-0.5 max-w-full truncate text-left font-mono text-[13px] font-medium text-ink hover:text-accent-strong">
                    {entry.path}{main ? <span className="text-ink-3"> → {main}</span> : null}
                  </button>
                  {entry.detail && <p className="mt-0.5 text-[12px] leading-snug text-ink-3">{entry.detail}</p>}
                </Fact>
              )}
              {core && (
                <Fact icon={Folder} label="Core folder" source={core.source} onOpen={open}>
                  <p className="mt-0.5 font-mono text-[13px] font-medium text-ink">
                    {core.path}/{core.files ? <span className="font-sans text-[12px] font-normal text-ink-3"> · {formatCount(core.files)} files</span> : null}
                  </p>
                  {core.detail && <p className="mt-0.5 text-[12px] leading-snug text-ink-3">{core.detail}</p>}
                </Fact>
              )}
            </div>
          )}

          {commands.length > 0 && (
            <div>
              <h3 className="mb-1.5 text-[12px] font-medium text-ink-3">Commands</h3>
              <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
                {commands.map((item) => (
                  <li key={`${item.kind}-${item.command}`} className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span className="w-[5.5rem] shrink-0 text-[12px] text-ink-3">{item.label}</span>
                      <code className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-ink" title={item.command}>{item.command}</code>
                      <CopyCommand command={item.command} />
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 pl-[6rem]">
                      {/* A script or recipe reads as code; an explanation as prose. */}
                      <p className={`min-w-0 flex-1 truncate text-[11.5px] text-ink-3 ${item.detail && !item.detail.endsWith('.') ? 'font-mono text-[11px]' : ''}`} title={item.detail || undefined}>{item.detail}</p>
                      <Cite source={item.source} onOpen={open} />
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {steps.length > 0 && (
          <div>
            <h3 className="mb-1.5 text-[12px] font-medium text-ink-3">Reading path</h3>
            <ol className="space-y-1" aria-label="Reading path">
              {steps.map((step, index) => (
                <li key={`${step.path}:${step.line}`}>
                  <button
                    type="button"
                    onClick={() => open(step)}
                    className="group flex w-full items-start gap-2.5 rounded-xl px-2 py-1.5 text-left transition-colors hover:bg-surface-2"
                    aria-label={`Step ${index + 1}: ${step.title}. Open ${step.path} at line ${step.line}`}
                  >
                    <span className="mt-px flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent-soft font-mono text-[11px] font-semibold text-accent-strong" aria-hidden="true">{index + 1}</span>
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-baseline gap-x-2">
                        <span className="text-[13px] font-medium text-ink">{step.title}</span>
                        <span className="truncate font-mono text-[11.5px] text-ink-3">{step.path}:{step.line}</span>
                      </span>
                      {step.detail && <span className="mt-0.5 block text-[12px] leading-snug text-ink-3">{step.detail}</span>}
                    </span>
                    <ArrowRight className="mt-1 h-3.5 w-3.5 shrink-0 text-ink-3 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>

      {guides.length > 0 && (
        <p className="mt-4 flex flex-wrap items-center gap-x-1.5 gap-y-1 border-t border-line pt-3 text-[12px] text-ink-3">
          <BookOpen className="h-3.5 w-3.5" aria-hidden="true" />
          <span>In the README:</span>
          {guides.map((guide, index) => (
            <span key={`${guide.path}:${guide.line}`} className="inline-flex items-center gap-1.5">
              {index > 0 && <span aria-hidden="true">·</span>}
              <button type="button" onClick={() => open(guide)} className="font-medium text-ink-2 underline-offset-2 hover:text-accent-strong hover:underline" aria-label={`Open ${guide.title} in ${guide.path} at line ${guide.line}`}>
                {guide.title}
              </button>
            </span>
          ))}
        </p>
      )}
    </section>
  );
};

export default StartHere;
