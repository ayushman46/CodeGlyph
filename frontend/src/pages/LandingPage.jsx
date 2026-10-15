import { motion, MotionConfig, useReducedMotion } from 'framer-motion';
import { ArrowRight, Check } from 'lucide-react';
import FeatureGrid from '../components/landing/FeatureGrid';
import HeroMockup from '../components/landing/HeroMockup';
import HowItWorks from '../components/landing/HowItWorks';
import LandingFooter from '../components/landing/LandingFooter';
import PullRequestSection from '../components/landing/PullRequestSection';
import Reveal, { EASE_OUT } from '../components/landing/Reveal';
import SecuritySection from '../components/landing/SecuritySection';
import StartFreeButton from '../components/landing/StartFreeButton';
import Button from '../components/ui/Button';
import useStore from '../store/useStore';

const TRUST_POINTS = ['Free Explorer plan', 'No credit card', 'Any public GitHub repository'];

const PROOF_POINTS = [
  {
    value: '10,000 files',
    label: 'per repository. Monorepos like cal.com fit. Important files index first; anything skipped is listed with the reason.',
  },
  {
    value: 'path:L12-L40',
    mono: true,
    label: 'in every answer. Click one and the exact lines open. Each reference is checked against the indexed code.',
  },
  {
    value: 'Moments',
    label: 'to keyword search. Semantic search builds in the background while you already ask questions.',
  },
  {
    value: 'Every hunk',
    label: 'validated against the real file before the diff reaches you for review.',
  },
];

/* Sign-in failures are reported by the site header (sticky, so the message
 * stays in view whichever of these buttons was used). */
const PrimaryActions = ({ user, size = 'lg', secondary }) => (
  <div className="flex w-full flex-col items-stretch justify-center gap-3 sm:w-auto sm:flex-row sm:items-center">
    {user ? (
      <Button variant="primary" size={size} to="/app" iconRight={ArrowRight}>
        Open workspace
      </Button>
    ) : (
      <StartFreeButton size={size} />
    )}
    {secondary}
  </div>
);

const Hero = () => {
  const user = useStore((state) => state.user);
  const reduceMotion = useReducedMotion();
  // Short and close together: the copy should be readable within ~0.5s.
  const rise = (delay) => ({
    initial: reduceMotion ? false : { opacity: 0, y: 12 },
    animate: { opacity: 1, y: 0 },
    transition: { duration: 0.5, ease: EASE_OUT, delay },
  });

  return (
    <section aria-labelledby="hero-title" className="relative isolate overflow-hidden">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0 -z-10">
        <div className="bg-grid absolute inset-0" />
        <div className="accent-glow absolute inset-x-0 top-0 h-[36rem]" />
      </div>

      <div className="content-shell pb-16 pt-12 sm:pb-24 sm:pt-16 lg:pt-20">
        <div className="mx-auto flex max-w-5xl flex-col items-center text-center">
          <motion.p
            {...rise(0)}
            className="inline-flex max-w-full items-center gap-2.5 rounded-full border border-line bg-surface/80 px-3 py-1 shadow-sm backdrop-blur"
          >
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent" aria-hidden="true" />
            {/* The header already names the product on phones, so the pill
                leads with the size claim there. */}
            <span className="eyebrow hidden text-accent-strong! sm:inline">Codebase Intel</span>
            <span className="hidden h-3.5 w-px bg-line-strong sm:block" aria-hidden="true" />
            <span className="text-xs font-medium text-ink-2">Monorepos up to 10,000 files</span>
          </motion.p>

          {/* Fluid size: three lines on phones; from sm up the first sentence
              keeps to one line, so the headline reads as two claims. */}
          <motion.h1
            {...rise(0.04)}
            id="hero-title"
            className="display-title mt-6 text-[clamp(2.125rem,10.6vw,2.5rem)] text-ink sm:text-[clamp(2.5rem,6.6vw,4.5rem)]"
          >
            Answers cite path:L12–L40.{' '}
            <span className="text-gradient block pb-[0.1em]">Fixes ship as PRs.</span>
          </motion.h1>

          <motion.p {...rise(0.08)} className="mt-5 max-w-2xl text-pretty text-base leading-relaxed text-ink-2 sm:text-lg">
            Point it at a public GitHub repo. Every answer cites the exact line range, and every reference is checked against
            the indexed code. Multi-file changes come back as an editable diff; one click opens the pull request.
          </motion.p>

          <motion.div {...rise(0.12)} className="mt-8 flex w-full flex-col items-center sm:mt-9 sm:w-auto">
            <PrimaryActions
              user={user}
              secondary={(
                <Button variant="ghost" size="lg" to="/docs" iconRight={ArrowRight}>
                  Read the docs
                </Button>
              )}
            />
            {!user && (
              <ul className="mt-5 flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 text-[13px] text-ink-2">
                {TRUST_POINTS.map((point) => (
                  <li key={point} className="flex items-center gap-1.5">
                    <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
                    {point}
                  </li>
                ))}
              </ul>
            )}
          </motion.div>
        </div>

        <div className="mt-12 sm:mt-16">
          <HeroMockup />
        </div>
      </div>
    </section>
  );
};

const ProofStrip = () => (
  <section aria-labelledby="proof-title" className="border-y border-line bg-surface/60">
    <div className="content-shell py-14 sm:py-16">
      <Reveal>
        <h2 id="proof-title" className="text-center text-sm font-semibold tracking-tight text-ink-2">
          Sized for real monorepos
        </h2>
        <ul className="mt-6 grid grid-cols-1 gap-px overflow-hidden rounded-2xl border border-line bg-line sm:mt-8 sm:grid-cols-2 lg:grid-cols-4">
          {PROOF_POINTS.map((point) => (
            <li key={point.value} className="bg-surface p-5 sm:p-6">
              <p className={`section-title text-ink ${point.mono ? 'font-mono text-[1.375rem]' : 'text-[1.625rem]'}`}>
                {point.value}
              </p>
              <p className="mt-2 text-sm leading-relaxed text-ink-2">{point.label}</p>
            </li>
          ))}
        </ul>
      </Reveal>
    </div>
  </section>
);

const FinalCta = () => {
  const user = useStore((state) => state.user);
  return (
    <section aria-labelledby="cta-title" className="py-20 sm:py-28">
      <div className="content-shell">
        <Reveal className="relative isolate overflow-hidden rounded-3xl border border-line bg-surface px-5 py-12 text-center shadow-[var(--shadow-soft)] sm:px-12 sm:py-20">
          <div aria-hidden="true" className="bg-grid absolute inset-0 -z-10" />
          <div aria-hidden="true" className="accent-glow absolute inset-0 -z-10" />
          <h2 id="cta-title" className="display-title mx-auto max-w-2xl text-balance text-[2rem] text-ink sm:text-5xl">
            Paste a repo URL. Get a cited answer.
          </h2>
          <p className="mx-auto mt-5 max-w-xl text-balance text-base leading-relaxed text-ink-2 sm:text-[17px]">
            Explorer is free with up to 200&nbsp;MB of indexed source. Team is ₹300 for 30&nbsp;days with up to 800&nbsp;MB.
          </p>
          <div className="mt-8 flex justify-center sm:mt-9">
            <PrimaryActions
              user={user}
              secondary={(
                <Button variant="ghost" size="lg" to="/pricing" iconRight={ArrowRight}>
                  Compare plans
                </Button>
              )}
            />
          </div>
        </Reveal>
      </div>
    </section>
  );
};

const LandingPage = () => (
  <MotionConfig reducedMotion="user">
    <main id="main" className="overflow-x-clip">
      <Hero />
      <ProofStrip />
      <FeatureGrid />
      <HowItWorks />
      <PullRequestSection />
      <SecuritySection />
      <FinalCta />
    </main>
    <LandingFooter />
  </MotionConfig>
);

export default LandingPage;
