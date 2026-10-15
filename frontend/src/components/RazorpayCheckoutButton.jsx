import { AlertCircle, ArrowRight, Check, LockKeyhole, RefreshCw } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { createTeamOrder, verifyTeamPayment } from '../api/client';
import useStore from '../store/useStore';
import { useGoogleSignIn } from './GoogleSignInButton';
import { buttonClass } from './ui/Button';
import Spinner from './ui/Spinner';

let razorpayScriptPromise;
const PENDING_CHECKOUT_KEY = 'codebase-intel.pending-team-checkout';
// Razorpay's hosted modal cannot read CSS variables, so pass the brand accent.
const RAZORPAY_THEME_COLOR = '#e4561b';

const loadRazorpayScript = () => {
  if (typeof window === 'undefined') return Promise.reject(new Error('Checkout is only available in a browser.'));
  if (window.Razorpay) return Promise.resolve();
  if (razorpayScriptPromise) return razorpayScriptPromise;

  razorpayScriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://checkout.razorpay.com/v1/checkout.js';
    script.async = true;
    script.onload = () => (window.Razorpay ? resolve() : reject(new Error('Razorpay checkout could not be loaded.')));
    script.onerror = () => reject(new Error('Razorpay checkout could not be loaded. Check your connection or ad blocker, then try again.'));
    document.body.appendChild(script);
  }).catch((error) => {
    razorpayScriptPromise = undefined;
    throw error;
  });

  return razorpayScriptPromise;
};

const providerError = (error, fallback = 'Team checkout could not be started. Please try again.') => {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (error?.message === 'Network Error') return 'Could not reach the server. It may be waking up; try again in a few seconds.';
  return error?.message || fallback;
};

const MESSAGE_TONE = {
  success: 'text-success',
  open: 'text-warning',
  verifying: 'text-ink-2',
  cancelled: 'text-ink-3',
};

/**
 * Team checkout. States: idle, loading (creating the order), open (Razorpay
 * modal on screen), verifying, success, cancelled, error (nothing charged)
 * and unconfirmed (Razorpay took the payment but our verification failed:
 * the user must not be nudged into paying twice).
 *
 * The default dark variant (bg-ink / text-canvas) keeps the label above WCAG
 * AA contrast in both themes; white on the accent orange is only ~3.7:1.
 */
const RazorpayCheckoutButton = ({
  className = '', onSuccess, variant = 'dark', size = 'lg', teamActive = false, renew = false,
}) => {
  const user = useStore((state) => state.user);
  const signIn = useGoogleSignIn();
  const [state, setState] = useState('idle');
  const [message, setMessage] = useState('');
  const [payment, setPayment] = useState(null);
  const mountedRef = useRef(true);
  const busyRef = useRef(false);
  // The reason Razorpay gave for the last failed attempt, kept so it is still
  // visible after the modal closes.
  const failureRef = useRef('');

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const update = (nextState, nextMessage = '') => {
    if (mountedRef.current) {
      setState(nextState);
      setMessage(nextMessage);
    }
  };

  const confirmPayment = async (response) => {
    busyRef.current = true;
    update('verifying', 'Confirming your payment with the server…');
    try {
      await verifyTeamPayment({
        razorpay_payment_id: response.razorpay_payment_id,
        razorpay_order_id: response.razorpay_order_id,
        razorpay_signature: response.razorpay_signature,
      });
      busyRef.current = false;
      if (mountedRef.current) setPayment(null);
      update('success', 'Team is active. Your workspace limit is now 800 MB for 30 days.');
      await onSuccess?.();
    } catch (error) {
      busyRef.current = false;
      if (mountedRef.current) setPayment(response);
      update('unconfirmed', providerError(error, 'Payment verification could not be completed.'));
    }
  };

  const handleCheckout = async () => {
    if (busyRef.current) return;
    if (!user) {
      // Keep the user's one-click intent across the OAuth redirect. Once the
      // session is restored, the effect below resumes checkout automatically.
      try {
        window.sessionStorage.setItem(PENDING_CHECKOUT_KEY, '1');
      } catch {
        // Storage can be disabled in privacy-restricted browsers; the user can
        // still return and click the button again after signing in.
      }
      update('idle');
      const signInStarted = await signIn.start();
      if (signInStarted === false) {
        try {
          window.sessionStorage.removeItem(PENDING_CHECKOUT_KEY);
        } catch {
          // Ignore storage failures; the visible error remains actionable.
        }
      }
      return;
    }

    busyRef.current = true;
    update('loading');
    try {
      const { data: order } = await createTeamOrder();
      // Prefer the server-provided public key. This prevents a stale/missing
      // Vite build variable from making an otherwise configured checkout look
      // inert. The key is intentionally public; the secret never leaves the
      // backend.
      const publicKey = String(order?.key_id || import.meta.env.VITE_RAZORPAY_KEY_ID || '').trim();
      if (!publicKey) {
        throw new Error('Team checkout is not configured for this environment.');
      }
      if (!order?.order_id || Number(order.amount) < 100 || order.currency !== 'INR') {
        throw new Error('The checkout order returned by the server is invalid.');
      }
      await loadRazorpayScript();

      const checkout = new window.Razorpay({
        key: publicKey,
        amount: order.amount,
        currency: order.currency,
        name: 'Codebase Intel',
        description: 'Team plan · 800 MB for 30 days',
        order_id: order.order_id,
        prefill: {
          name: user.user_metadata?.full_name || '',
          email: user.email || '',
        },
        theme: { color: RAZORPAY_THEME_COLOR },
        handler: (response) => confirmPayment(response),
        modal: {
          ondismiss: () => {
            busyRef.current = false;
            const failure = failureRef.current;
            failureRef.current = '';
            if (failure) update('error', `${failure} Nothing was charged.`);
            else update('cancelled', 'Checkout closed. Nothing was charged.');
          },
        },
      });

      failureRef.current = '';
      checkout.on('payment.failed', (response) => {
        const description = String(response?.error?.description || '').trim();
        failureRef.current = description ? `Payment failed: ${description.replace(/\.?$/, '.')}` : 'Payment failed.';
        // Keep the trigger locked while Razorpay's modal is still open. The
        // user can retry inside the modal; ondismiss releases the guard.
        update('open', `${failureRef.current} Try another method in the Razorpay window, or close it.`);
      });
      checkout.open();
      update('open');
    } catch (error) {
      busyRef.current = false;
      update('error', providerError(error));
    }
  };

  useEffect(() => {
    if (!user || busyRef.current) return;
    let pending = false;
    try {
      pending = window.sessionStorage.getItem(PENDING_CHECKOUT_KEY) === '1';
      if (pending) window.sessionStorage.removeItem(PENDING_CHECKOUT_KEY);
    } catch {
      pending = false;
    }
    // Someone who already has Team does not need the checkout they asked
    // for before signing in.
    if (pending && !teamActive) void handleCheckout();
    // handleCheckout reads the latest user from this render; resume only when
    // the signed-in user changes (for example right after the OAuth redirect).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  // An active Team plan has nothing to buy: paying again would restart the
  // 30 days from the new payment, not add to them.
  if (teamActive && state !== 'success') {
    return (
      <div>
        <p className="flex h-12 items-center justify-center gap-2 rounded-xl border border-success/25 bg-success-soft px-4 text-[15px] font-medium text-success">
          <Check className="h-4 w-4" aria-hidden="true" />
          Team is active on your workspace
        </p>
        <p className="mt-3 text-center text-xs leading-relaxed text-ink-3">
          It runs for 30 days from your payment. Buy it again once it ends; each payment starts a new 30 days.
        </p>
      </div>
    );
  }

  const redirecting = !user && signIn.busy;
  const busy = state === 'loading' || state === 'verifying' || state === 'open' || redirecting;
  const disabled = busy || state === 'success' || state === 'unconfirmed';
  const price = renew ? 'Renew Team · ₹300' : 'Choose Team · ₹300';
  const label = redirecting
    ? 'Redirecting to Google…'
    : state === 'loading'
      ? 'Preparing checkout…'
      : state === 'open'
        ? 'Finish in the Razorpay window…'
        : state === 'verifying'
          ? 'Confirming payment…'
          : state === 'success'
            ? 'Team plan active'
            : state === 'unconfirmed'
              ? 'Payment awaiting confirmation'
              : user
                ? price
                : 'Sign in to choose Team';

  const icon = busy
    ? <Spinner className="h-4 w-4" />
    : state === 'success'
      ? <Check className="h-4 w-4" aria-hidden="true" />
      : null;

  const signInError = !user && signIn.error ? `Google sign-in did not start: ${signIn.error}` : '';
  const errorMessage = state === 'error' ? message : signInError;
  const liveMessage = state !== 'error' && state !== 'unconfirmed' ? message : '';

  return (
    <div>
      <button
        type="button"
        onClick={handleCheckout}
        disabled={disabled}
        aria-busy={busy || undefined}
        data-testid="choose-team-button"
        className={buttonClass(variant, size, `group w-full ${className}`)}
      >
        {icon}
        {label}
        {!icon && state !== 'unconfirmed' && (
          <ArrowRight className="h-4 w-4 transition-transform duration-150 group-hover:translate-x-0.5" aria-hidden="true" />
        )}
      </button>
      <p className="mt-3 flex items-center justify-center gap-1.5 text-xs text-ink-3">
        <LockKeyhole className="h-3.5 w-3.5" aria-hidden="true" />
        Secure checkout by Razorpay
      </p>
      {/* A persistent live region announces progress; errors use an alert. */}
      <p
        role="status"
        aria-live="polite"
        className={liveMessage ? `mt-3 text-center text-sm leading-relaxed ${MESSAGE_TONE[state] || 'text-ink-2'}` : 'sr-only'}
      >
        {liveMessage}
      </p>
      {errorMessage && (
        <div role="alert" className="mt-3 flex items-start gap-2.5 rounded-xl border border-danger/25 bg-danger-soft px-3.5 py-3 text-sm leading-relaxed text-danger">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className="break-words">{errorMessage}</p>
          </div>
        </div>
      )}
      {state === 'unconfirmed' && (
        <div role="alert" className="mt-3 rounded-xl border border-warning/30 bg-warning-soft px-3.5 py-3 text-sm leading-relaxed text-ink">
          <p className="flex items-start gap-2.5 font-semibold text-warning">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            Razorpay accepted the payment, but we could not confirm it yet.
          </p>
          <p className="mt-1.5 text-[13px] text-ink-2">
            {message} Do not pay again. Retry the confirmation; it is safe to repeat.
            {payment?.razorpay_payment_id && (
              <> Payment ID <code className="break-all font-mono text-[12px] text-ink">{payment.razorpay_payment_id}</code>.</>
            )}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => payment && confirmPayment(payment)}
              className={buttonClass('secondary', 'sm')}
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              Retry confirmation
            </button>
            <button
              type="button"
              onClick={() => { setPayment(null); update('idle'); }}
              className={buttonClass('ghost', 'sm')}
            >
              Dismiss
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default RazorpayCheckoutButton;
