import { Link } from 'react-router-dom';
import GoogleSignInButton from '../components/GoogleSignInButton';
import useStore from '../store/useStore';

/*
 * The landing page is type and space only: one headline split on its
 * meaning, one plain sentence about what the product does, one button, one
 * reassurance line, and a footer you only notice when you look for it.
 */

/* The primary pill. accent-ink keeps the label at AA in both themes (white
   on the dark theme's lighter orange would not be). */
const PILL = 'inline-flex h-12 items-center rounded-full bg-accent px-7 text-[15px] font-medium text-accent-ink transition-[background-color] duration-(--duration-fast) hover:bg-accent-strong focus-visible:outline-offset-[3px]';

/* GoogleSignInButton renders the "secondary" Button; these overrides turn it
   into the same accent pill. The Google "G" sits on its own white disc, as
   Google's branding asks for on coloured buttons, so the left padding is
   sized for that disc. While redirecting, the button is disabled and busy:
   the disc is replaced by a spinner, so the padding evens out again, and
   hover/press stay inert (as in Button's own state model). A failed sign-in
   is reported once, by GoogleSignInButton's own toast, never inline here. */
const GOOGLE_PILL = [
  'h-12! rounded-full! border-transparent! bg-accent! pl-2! pr-6! gap-3! text-[15px]! text-accent-ink! shadow-none!',
  'aria-busy:pl-6! aria-busy:gap-2.5!',
  'not-disabled:hover:bg-accent-strong! not-disabled:active:bg-accent-strong! focus-visible:outline-offset-[3px]',
  '[&>svg:not(.animate-spin)]:box-content [&>svg:not(.animate-spin)]:rounded-full [&>svg:not(.animate-spin)]:bg-white [&>svg:not(.animate-spin)]:p-2',
].join(' ');

/* Only ever the first name: the email address must not appear in large-ish
   text on a page people screenshot and screen-share. */
const firstName = (user) => {
  const full = user?.user_metadata?.full_name;
  return typeof full === 'string' ? full.trim().split(/\s+/)[0] : '';
};

const LandingPage = () => {
  const user = useStore((state) => state.user);
  const name = firstName(user);

  return (
    /* The sticky header is 56px plus a 1px hairline; subtracting both keeps
       the page from scrolling by a single pixel. */
    <div className="flex min-h-[calc(100svh-3.5rem-1px)] flex-col">
      <main id="main" className="flex flex-1 items-center justify-center px-4 pb-20 pt-12 sm:px-6 sm:pb-28 sm:pt-16">
        <div className="flex w-full max-w-5xl flex-col items-center text-center">
          {/* Regular Inter (already loaded for the header), so the headline
              never redraws in a different cut while a font arrives. Capped
              at 72px: large enough to lead, small enough to stay calm. */}
          <h1 className="text-[clamp(2.625rem,1.5rem+4.2vw,4.5rem)] font-medium leading-[1.03] tracking-[-0.045em] text-ink">
            <span className="block">Know the code</span>{' '}
            <span className="block text-balance">before you change it.</span>
          </h1>

          {/* One sentence per line from sm up, so the lines break on meaning
              (like the headline); on phones it wraps as a normal paragraph. */}
          <p className="mt-6 max-w-[40rem] text-pretty text-[17px] leading-[1.6] text-ink-2 sm:mt-8 sm:text-[19px]">
            <span className="sm:block">Ask anything about a public GitHub repository.</span>{' '}
            <span className="sm:block">Answers cite the exact lines. Changes become pull requests.</span>
          </p>

          <div className="mt-9 sm:mt-11">
            {user ? (
              <Link to="/app" className={PILL}>Open your workspace</Link>
            ) : (
              <GoogleSignInButton size="lg" label="Continue with Google" className={GOOGLE_PILL} />
            )}
          </div>

          <p className="mt-4 text-[13px] text-ink-3">
            {user ? (
              name ? `Signed in as ${name}` : 'You are signed in'
            ) : (
              <>
                Free plan<span className="sr-only">.</span>
                <span aria-hidden="true" className="px-1.5">·</span>
                No card needed
              </>
            )}
          </p>
        </div>
      </main>

      <footer className="px-4 pb-[max(1.5rem,env(safe-area-inset-bottom))] text-center text-[13px] text-ink-3 sm:px-6">
        <p>&copy; {new Date().getFullYear()} Codebase Intel</p>
      </footer>
    </div>
  );
};

export default LandingPage;
