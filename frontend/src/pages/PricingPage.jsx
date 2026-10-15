import {
  ArrowRight, Check, ChevronDown, Code2, GitPullRequest, Layers, Link2, Minus, Quote, Search, ShieldCheck,
} from 'lucide-react';
import { Fragment, useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import GoogleSignInButton from '../components/GoogleSignInButton';
import RazorpayCheckoutButton from '../components/RazorpayCheckoutButton';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import useStore from '../store/useStore';

const STORAGE_STEPS = 8; // one segment per 100 MB of indexed source

const PLANS = [
  {
    id: 'explorer',
    name: 'Explorer',
    tagline: 'The whole product, with 200 MB of indexed source for your workspace.',
    price: 'Free',
    priceNote: 'No card needed. Sign in with Google.',
    storage: '200 MB',
    storageSteps: 2,
    features: [
      'Index public GitHub repositories, up to 30 per account',
      '200 MB of indexed source across all of your repositories',
      'Streaming answers with path:line citations checked against the index',
      'Code search, repository overview and impact analysis',
      'Multi-file diffs you review and edit, opened as GitHub pull requests',
      'Private workspace scoped to your Google account',
    ],
  },
  {
    id: 'team',
    name: 'Team',
    tagline: '4× the indexed source: room for larger repositories, or several at once.',
    priceAmount: '₹300',
    pricePeriod: '/ month',
    priceNote: ['One-time payment', '30 days of Team', 'no auto-renewal'],
    storage: '800 MB',
    storageSteps: 8,
    recommended: true,
    features: [
      'Everything in Explorer',
      '800 MB of indexed source across all of your repositories',
      '30 days of Team per payment, counted from the day you pay',
      'Paid once through Razorpay; nothing renews or charges again',
    ],
  },
];

// Rows of the side-by-side comparison. `true` renders a check, a string is
// shown as-is, and `null` renders a dash.
const COMPARISON = [
  { label: 'Price', explorer: 'Free', team: '₹300 one-time, for 30 days' },
  { label: 'Indexed source, whole workspace', explorer: '200 MB', team: '800 MB', strong: true },
  { label: 'Budget per repository', explorer: '10,000 files · 100 MB · 15,000 sections', team: 'Same' },
  { label: 'Repositories per account', explorer: 'Up to 30, public GitHub only', team: 'Same' },
  { label: 'Cited answers, code search, overview, impact', explorer: true, team: true },
  { label: 'Proposed changes and pull requests', explorer: 'Up to 8 files per change', team: 'Same' },
  { label: 'Renewal', explorer: null, team: 'None. Buy again when the 30 days end.' },
];

const INCLUDED = [
  { icon: Layers, title: 'Large-repository indexing', text: 'Files are ranked by importance and indexed within a budget, so a big repository is indexed instead of rejected.' },
  {
    icon: Quote,
    title: 'Cited, streaming answers',
    text: <>Answers cite code as <code className="whitespace-nowrap font-mono text-[0.9em] text-ink-2">path:L12-L40</code> and open those exact lines in a built-in viewer.</>,
  },
  { icon: Search, title: 'Code search', text: 'Keyword search works moments after you add a repository; the semantic index builds in the background.' },
  { icon: Code2, title: 'Six question workflows', text: 'General, onboarding, security review, architecture, contributor and due diligence.' },
  { icon: GitPullRequest, title: 'Reviewed pull requests', text: 'Every hunk is validated against the real file. Edit the diff, then push a PR or download a .patch.' },
  { icon: ShieldCheck, title: 'Account-scoped security', text: 'Workspaces are scoped to your account, and GitHub tokens are encrypted at rest.' },
];

const FAQ = [
  {
    id: 'storage',
    question: 'What counts toward my storage?',
    answer: 'Only source that is actually indexed. Usage is the total size of indexed files across all of your repositories. Skipped files, build artifacts and the temporary download do not count, and deleting a repository frees its space immediately.',
  },
  {
    id: 'limit',
    question: 'What happens when I reach my limit?',
    answer: 'Nothing you already indexed is removed, and questions keep working. A repository added with little space left gets its most important files indexed up to the space remaining; the rest are listed in its coverage report. When no space is left, new indexing stops with a message. Delete a repository or move to Team to make room.',
  },
  {
    id: 'large-repositories',
    question: 'What happens with a very large repository?',
    answer: 'It is still indexed. Files are ranked by importance (source, schema and config first; tests, fixtures, generated code and data later) and indexed until the per-repository budget is spent: 10,000 files, 100 MB or 15,000 sections by default. Every omitted file is listed with its reason in the coverage report.',
  },
  {
    id: 'renewal',
    question: 'Does the Team plan renew automatically?',
    answer: 'No. Team is a one-time Razorpay payment of ₹300 that raises your limit to 800 MB for 30 days from the payment. When the 30 days end, the Explorer limit applies again and your indexed repositories stay searchable. Buy Team again whenever you need the room.',
  },
  {
    id: 'privacy',
    question: 'Is my code private?',
    answer: 'Your workspace is scoped to your account: the repositories you add, your conversations and your usage are visible only to you. If you connect GitHub to open pull requests, the token is encrypted at rest, and nothing is pushed without your review.',
  },
  {
    id: 'private-repositories',
    question: 'Can I index private repositories?',
    answer: 'Not today. Codebase Intel indexes public GitHub repositories only. Connecting GitHub is used only to open pull requests from changes you have reviewed.',
  },
  {
    id: 'github-account',
    question: 'Do I need a GitHub account?',
    answer: 'Only to open a pull request. Indexing, questions, search and diff review work with Google sign-in alone. Connect GitHub from the review screen when you are ready to push, or download the change as a .patch instead.',
  },
];

const StorageMeter = ({ steps, label }) => (
  <div>
    <div className="flex items-baseline justify-between gap-3 text-[13px]">
      <span className="text-ink-2">Indexed source</span>
      <span className="font-mono font-medium text-ink">{label}</span>
    </div>
    <div className="mt-2 grid grid-cols-8 gap-1" aria-hidden="true">
      {Array.from({ length: STORAGE_STEPS }, (_, index) => (
        <span key={index} className={`h-1.5 rounded-full ${index < steps ? 'bg-accent' : 'bg-surface-3'}`} />
      ))}
    </div>
  </div>
);

const PlanCard = ({ plan, cta, current }) => {
  const highlighted = Boolean(plan.recommended);
  const headingId = `plan-${plan.id}-title`;
  return (
    <article
      aria-labelledby={headingId}
      className={`relative flex min-w-0 flex-col rounded-3xl border bg-surface p-6 sm:p-8 ${highlighted
        ? 'border-accent/45 shadow-[var(--shadow-lift)] ring-1 ring-accent/20'
        : 'border-line shadow-[var(--shadow-soft)]'}`}
    >
      {highlighted && (
        <div className="accent-glow pointer-events-none absolute inset-x-0 top-0 h-40 rounded-t-3xl opacity-70" aria-hidden="true" />
      )}
      <div className="relative flex flex-wrap items-center justify-between gap-3">
        <h2 id={headingId} className="section-title text-2xl text-ink">{plan.name}</h2>
        {current && <Badge tone="success" dot>Your current plan</Badge>}
      </div>
      <p className="relative mt-2 text-[15px] leading-relaxed text-ink-2">{plan.tagline}</p>

      <div className="relative mt-7">
        {plan.price ? (
          <p className="display-title text-5xl text-ink">{plan.price}</p>
        ) : (
          <p className="flex flex-wrap items-baseline gap-x-1.5 text-ink">
            <span className="display-title text-5xl">{plan.priceAmount}</span>
            <span className="text-base font-medium text-ink-3"> {plan.pricePeriod}</span>
          </p>
        )}
        <p className="mt-2 text-[13px] text-ink-2">
          {/* Each phrase stays on one line so a wrap never splits "auto-renewal". */}
          {[].concat(plan.priceNote).map((part, index) => (
            <Fragment key={part}>{index > 0 && ' · '}<span className="whitespace-nowrap">{part}</span></Fragment>
          ))}
        </p>
      </div>

      <div className="relative mt-6 rounded-2xl border border-line bg-surface-2/70 p-4">
        <StorageMeter steps={plan.storageSteps} label={plan.storage} />
      </div>

      <div className="relative mt-6">{cta}</div>

      <div className="relative mt-8 border-t border-line pt-6">
        <h3 className="text-[13px] font-semibold text-ink">What’s included</h3>
        <ul className="mt-4 space-y-3">
          {plan.features.map((feature) => (
            <li key={feature} className="flex gap-3 text-sm leading-relaxed text-ink-2">
              <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${highlighted ? 'bg-accent text-accent-ink' : 'bg-accent-soft text-accent-strong'}`}>
                <Check className="h-3 w-3" strokeWidth={3} aria-hidden="true" />
              </span>
              <span className="min-w-0">{feature}</span>
            </li>
          ))}
        </ul>
      </div>
    </article>
  );
};

/** Signed-in visitors see where they stand before comparing plans. */
const CurrentUsage = ({ usage }) => {
  const isTeam = usage.plan === 'team';
  const expired = usage.status === 'expired';
  return (
    <div className="mx-auto mb-6 flex max-w-5xl flex-col gap-3 rounded-2xl border border-line bg-surface/90 px-4 py-3 text-sm shadow-[var(--shadow-soft)] backdrop-blur sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <p className="min-w-0 leading-relaxed text-ink-2">
        <span className="font-semibold text-ink">You are on {isTeam ? 'Team' : 'Explorer'}.</span>{' '}
        <span className="whitespace-nowrap font-mono text-[13px] text-ink">{usage.used_label}</span> of{' '}
        <span className="whitespace-nowrap font-mono text-[13px] text-ink">{usage.quota_label}</span> used,{' '}
        <span className="whitespace-nowrap">{usage.remaining_label} left.</span>
        {expired && <span className="mt-1 block text-warning">Your 30 days of Team ended, so the Explorer limit applies again.</span>}
      </p>
      <Link to="/account" className="inline-flex shrink-0 items-center gap-1 self-start rounded-md text-[13px] font-medium text-accent-strong hover:underline sm:self-auto">
        Usage details <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
      </Link>
    </div>
  );
};

const ComparisonValue = ({ value, strong }) => {
  if (value === true) {
    return (
      <span className="inline-flex items-center gap-1.5 text-ink">
        <Check className="h-4 w-4 text-success" strokeWidth={2.5} aria-hidden="true" />
        Included
      </span>
    );
  }
  if (value === null) {
    return (
      <span className="inline-flex text-ink-3">
        <Minus className="h-4 w-4" aria-hidden="true" />
        <span className="sr-only">Not applicable</span>
      </span>
    );
  }
  return <span className={strong ? 'font-mono font-semibold text-ink' : 'text-ink-2'}>{value}</span>;
};

const Comparison = () => (
  <section className="content-shell relative mt-16 sm:mt-20" aria-labelledby="compare-title">
    <div className="mx-auto max-w-5xl">
      <h2 id="compare-title" className="section-title text-2xl text-ink sm:text-3xl">Compare the limits</h2>
      <p className="mt-2 max-w-2xl text-[15px] leading-relaxed text-ink-2">
        The plans differ in one number: how much indexed source your workspace holds. Every other limit is the same.
      </p>
      {/* A three-column table from sm up; labelled rows on phones, where
          three columns would squeeze each value into a sliver. Only one of
          the two is ever displayed, so assistive tech reads it once. */}
      <div className="mt-6 overflow-hidden rounded-2xl border border-line bg-surface shadow-[var(--shadow-soft)]">
        <table className="hidden w-full text-left text-sm sm:table">
          <caption className="sr-only">Explorer and Team plan limits</caption>
          <thead className="bg-surface-2 text-[13px]">
            <tr>
              <th scope="col" className="w-[36%] px-5 py-3 font-medium text-ink-2">Limit</th>
              <th scope="col" className="px-5 py-3 font-semibold text-ink">Explorer</th>
              <th scope="col" className="px-5 py-3 font-semibold text-ink">Team</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {COMPARISON.map((row) => (
              <tr key={row.label}>
                <th scope="row" className="px-5 py-3.5 align-top font-normal text-ink-2">{row.label}</th>
                <td className="px-5 py-3.5 align-top"><ComparisonValue value={row.explorer} strong={row.strong} /></td>
                <td className="px-5 py-3.5 align-top"><ComparisonValue value={row.team} strong={row.strong} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <ul className="divide-y divide-line sm:hidden" aria-label="Explorer and Team plan limits">
          {COMPARISON.map((row) => (
            <li key={row.label} className="px-4 py-3.5 text-sm">
              <p className="text-[13px] font-medium text-ink">{row.label}</p>
              <dl className="mt-1.5 grid grid-cols-2 gap-3">
                {[['Explorer', row.explorer], ['Team', row.team]].map(([plan, value]) => (
                  <div key={plan} className="min-w-0">
                    <dt className="text-[11px] font-medium uppercase tracking-wide text-ink-3">{plan}</dt>
                    <dd className="mt-0.5 leading-snug"><ComparisonValue value={value} strong={row.strong} /></dd>
                  </div>
                ))}
              </dl>
            </li>
          ))}
        </ul>
      </div>
    </div>
  </section>
);

const FaqItem = ({ id, question, answer, open }) => (
  <details id={`faq-${id}`} className="group scroll-mt-24" open={open || undefined}>
    <summary className="flex cursor-pointer list-none items-center justify-between gap-4 rounded-md py-5 text-left text-[15px] font-medium text-ink transition-colors hover:text-accent-strong [&::-webkit-details-marker]:hidden">
      <span className="min-w-0">{question}</span>
      <ChevronDown className="h-4 w-4 shrink-0 text-ink-3 transition-transform duration-200 group-open:rotate-180" aria-hidden="true" />
    </summary>
    <div className="-mt-1 pb-5 pr-2 sm:pr-8">
      <p className="text-sm leading-relaxed text-ink-2">{answer}</p>
      <a
        href={`#faq-${id}`}
        className="mt-2 inline-flex items-center gap-1 rounded text-xs text-ink-3 transition-colors hover:text-ink"
        aria-label={`Link to this answer: ${question}`}
      >
        <Link2 className="h-3 w-3" aria-hidden="true" />
        Link to this answer
      </a>
    </div>
  </details>
);

const PricingPage = () => {
  const user = useStore((state) => state.user);
  const accountUsage = useStore((state) => state.accountUsage);
  const fetchAccountUsage = useStore((state) => state.fetchAccountUsage);
  const location = useLocation();
  const hash = location.hash.replace(/^#/, '');

  // Signed-in visitors see which plan is active; the store keeps the last
  // known value if this background refresh fails.
  useEffect(() => {
    if (user?.id) void fetchAccountUsage({ silent: true });
  }, [user?.id, fetchAccountUsage]);

  // The page is lazy-loaded, so the browser cannot scroll to a deep-linked
  // answer by itself on first load.
  useEffect(() => {
    if (!hash) return;
    const target = document.getElementById(hash);
    if (target) target.scrollIntoView({ block: 'start' });
  }, [hash]);

  const usage = user ? accountUsage : null;
  const currentPlan = usage ? usage.plan : null;
  const teamActive = currentPlan === 'team';

  const explorerCta = user ? (
    <Button to="/app" variant="secondary" size="lg" className="w-full" iconRight={ArrowRight}>
      Open workspace
    </Button>
  ) : (
    <GoogleSignInButton size="lg" className="w-full" label="Start free with Google" />
  );
  const teamCta = (
    <RazorpayCheckoutButton
      teamActive={teamActive}
      renew={usage?.status === 'expired'}
      onSuccess={() => fetchAccountUsage()}
    />
  );

  return (
    <main className="relative overflow-hidden pb-20 sm:pb-28">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-[34rem]" aria-hidden="true">
        <div className="bg-grid absolute inset-0 opacity-70" />
        <div className="accent-glow absolute inset-0" />
      </div>

      <section className="content-shell relative pt-16 text-center sm:pt-24" aria-labelledby="pricing-title">
        <p className="eyebrow animate-fade-in">Pricing</p>
        <h1 id="pricing-title" className="display-title mx-auto mt-4 max-w-3xl animate-rise text-balance text-[2.5rem] text-ink sm:text-6xl">
          Choose the right place to start.
        </h1>
        <p className="mx-auto mt-5 max-w-2xl animate-rise text-pretty text-base leading-relaxed text-ink-2 sm:text-lg">
          Explorer is free and includes every feature. Team gives your workspace four times the indexed source for 30
          days, paid once. No subscription, nothing renews.
        </p>
      </section>

      <section className="content-shell relative mt-12 sm:mt-16" aria-label="Plans">
        {usage && <CurrentUsage usage={usage} />}
        <div className="mx-auto grid max-w-5xl animate-rise gap-5 md:grid-cols-2 md:gap-6">
          {PLANS.map((plan) => (
            <PlanCard
              key={plan.id}
              plan={plan}
              current={currentPlan === plan.id}
              cta={plan.id === 'team' ? teamCta : explorerCta}
            />
          ))}
        </div>
      </section>

      <Comparison />

      <section className="content-shell relative mt-20 sm:mt-28" aria-labelledby="included-title">
        <div className="mx-auto max-w-5xl">
          <div className="max-w-2xl">
            <p className="eyebrow">Every plan</p>
            <h2 id="included-title" className="section-title mt-3 text-balance text-3xl text-ink sm:text-4xl">The whole product, on both plans.</h2>
            <p className="mt-4 text-base leading-relaxed text-ink-2">
              No feature is held back for Team. You pay only for more indexed source.
            </p>
          </div>
          <ul className="mt-10 grid gap-px overflow-hidden rounded-3xl border border-line bg-line sm:grid-cols-2 lg:grid-cols-3">
            {INCLUDED.map(({ icon: Icon, title, text }) => (
              <li key={title} className="bg-surface p-6">
                <span className="flex h-9 w-9 items-center justify-center rounded-xl border border-line bg-surface-2 text-accent">
                  <Icon className="h-4 w-4" aria-hidden="true" />
                </span>
                <h3 className="mt-4 text-[15px] font-semibold text-ink">{title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-ink-3">{text}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section className="content-shell relative mt-20 sm:mt-28" aria-labelledby="faq-title">
        <div className="mx-auto grid max-w-5xl gap-10 lg:grid-cols-[18rem_minmax(0,1fr)] lg:gap-16">
          <div>
            <p className="eyebrow">FAQ</p>
            <h2 id="faq-title" className="section-title mt-3 text-balance text-3xl text-ink sm:text-4xl">Questions, answered.</h2>
            <p className="mt-4 text-base leading-relaxed text-ink-2">
              Storage, limits, privacy and billing. For how indexing works in detail, read the{' '}
              <Link to="/docs#indexing" className="font-medium text-accent-strong underline-offset-2 hover:underline">indexing guide</Link>.
            </p>
          </div>
          <div className="divide-y divide-line border-y border-line">
            {FAQ.map((item) => <FaqItem key={item.id} {...item} open={hash === `faq-${item.id}`} />)}
          </div>
        </div>
      </section>
    </main>
  );
};

export default PricingPage;
