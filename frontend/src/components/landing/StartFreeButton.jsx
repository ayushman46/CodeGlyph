import GoogleSignInButton from '../GoogleSignInButton';

/* The landing page's primary sign-up action. GoogleSignInButton defaults to
 * the white secondary style, which is almost invisible as a primary CTA on
 * the dark canvas; the ink fill (near-black in light mode, near-white in dark
 * mode) is the strongest neutral in both themes and matches the header's
 * Sign in button. Both are Google-approved button themes for the logo. */
const INK = 'border-transparent! bg-ink! text-canvas! hover:opacity-90';

const StartFreeButton = ({ size = 'lg', label = 'Start free with Google', className = '' }) => (
  <GoogleSignInButton size={size} label={label} className={`${INK} ${className}`} />
);

export default StartFreeButton;
