import { Check, KeyRound, ShieldAlert, Ticket, UserRound } from 'lucide-react';
import Reveal from './Reveal';
import SectionHeading from './SectionHeading';

const SAFEGUARDS = [
  {
    icon: UserRound,
    title: 'Account-scoped workspaces',
    body: 'Your repositories, history and workspace are scoped to the account you sign in with.',
  },
  {
    icon: KeyRound,
    title: 'Encrypted GitHub tokens',
    body: 'GitHub access tokens are stored encrypted at rest, using authenticated encryption.',
  },
  {
    icon: Ticket,
    title: 'File-scoped PR tickets',
    body: 'Every pull request action needs a short-lived, signed ticket bound to your account, the repository and the exact files.',
  },
  {
    icon: ShieldAlert,
    title: 'Untrusted content stays data',
    body: 'Code, comments, docs and issues are treated as untrusted input. The assistant is instructed never to follow instructions inside them.',
  },
];

const TICKET_ROWS = [
  { label: 'Account', value: 'Your workspace' },
  { label: 'Repository', value: 'calcom/cal.com', mono: true },
  { label: 'Files', value: 'handleConfirmation.ts\nhandleConfirmation.test.ts', mono: true },
  { label: 'Expires', value: '10 minutes after issue' },
];

const TicketMock = () => (
  <figure className="mt-10 max-w-md">
    <figcaption className="sr-only">
      Example pull request ticket scoped to one account, the calcom/cal.com repository and two files, expiring 10 minutes after
      it is issued, with a verified signature.
    </figcaption>
    <div aria-hidden="true" className="relative overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-soft)]">
      <div className="flex items-center justify-between gap-3 border-b border-dashed border-line-strong px-4 py-3">
        <span className="flex items-center gap-2 text-[13px] font-semibold text-ink">
          <Ticket className="h-4 w-4 text-accent-strong" />
          Pull request ticket
        </span>
        <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[10.5px] font-medium text-accent-strong">Short-lived</span>
      </div>
      <dl className="space-y-2 px-4 py-3 text-[12px]">
        {TICKET_ROWS.map((row) => (
          <div key={row.label} className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3">
            <dt className="text-ink-3">{row.label}</dt>
            <dd className={`whitespace-pre-line break-words text-ink ${row.mono ? 'font-mono text-[11.5px]' : ''}`}>{row.value}</dd>
          </div>
        ))}
      </dl>
      <div className="flex items-center gap-1.5 border-t border-line bg-surface-2/60 px-4 py-2.5 text-[11.5px] text-ink-2">
        <Check className="h-3.5 w-3.5 text-success" />
        Signature checked on every pull request action
      </div>
    </div>
  </figure>
);

const SecuritySection = () => (
  <section id="security" aria-labelledby="security-title" className="scroll-mt-20 border-y border-line bg-surface-2/50 py-20 sm:py-28">
    <div className="content-shell grid grid-cols-1 gap-12 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:gap-16">
      <Reveal>
        <SectionHeading
          id="security-title"
          align="left"
          eyebrow="Security"
          title="Scoped access from sign-in to pull request"
          description="Codebase Intel reads code you do not control and can open pull requests for you, so access is scoped tightly at every step."
        />
        <div className="hidden lg:block">
          <TicketMock />
        </div>
      </Reveal>

      <ul className="grid grid-cols-1 content-start gap-3 sm:grid-cols-2 sm:gap-4">
        {SAFEGUARDS.map(({ icon: Icon, title, body }, index) => (
          <Reveal
            as="li"
            key={title}
            delay={(index % 2) * 0.06}
            className="rounded-2xl border border-line bg-surface p-5 shadow-[var(--shadow-soft)] sm:p-6"
          >
            <div className="flex items-center gap-3 sm:block">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-strong">
                <Icon className="h-[18px] w-[18px]" aria-hidden="true" />
              </span>
              <h3 className="text-[15px] font-semibold tracking-tight text-ink sm:mt-4">{title}</h3>
            </div>
            <p className="mt-2 text-sm leading-relaxed text-ink-2 sm:mt-1.5">{body}</p>
          </Reveal>
        ))}
      </ul>
    </div>
  </section>
);

export default SecuritySection;
