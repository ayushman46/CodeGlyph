// The app only uses Supabase for sign-in, so it loads the auth client alone
// rather than @supabase/supabase-js, which also bundles the database,
// storage, realtime and functions clients on every page.
import { AuthClient } from '@supabase/auth-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || '';
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || '';

const isValidUrl = (url) => {
  try {
    new URL(url);
    return true;
  } catch {
    return false;
  }
};

/*
 * End-to-end tests run a dedicated build with VITE_E2E_FAKE_SESSION=true so
 * the workspace can be exercised against mocked API routes without Google
 * sign-in. Vite replaces the flag at build time; production builds never set
 * it, so this branch is removed from the shipped bundle.
 */
const E2E_SESSION = import.meta.env.VITE_E2E_FAKE_SESSION === 'true'
  ? {
    access_token: 'e2e-access-token',
    token_type: 'bearer',
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: {
      id: 'e2e-user',
      email: 'tester@example.test',
      created_at: '2026-01-01T00:00:00Z',
      user_metadata: { full_name: 'E2E Tester' },
    },
  }
  : null;

const sessionClient = (session) => ({
  auth: {
    getSession: async () => ({ data: { session } }),
    onAuthStateChange: () => ({ data: { subscription: { unsubscribe: () => {} } } }),
    signInWithOAuth: async () => ({ error: session ? null : new Error('Supabase authentication is not configured.') }),
    signOut: async () => ({ error: null }),
  },
});

export const isSupabaseConfigured = Boolean(E2E_SESSION) || Boolean(supabaseUrl && supabaseAnonKey && isValidUrl(supabaseUrl));

/*
 * Configured exactly as createClient() configures its auth client (same URL,
 * headers, defaults and storage key), so sessions saved by earlier builds
 * keep working and the OAuth redirect flow is unchanged.
 */
const authOnlyClient = (url, key) => {
  const base = new URL(url.endsWith('/') ? url : `${url}/`);
  return {
    auth: new AuthClient({
      url: new URL('auth/v1', base).href,
      headers: { Authorization: `Bearer ${key}`, apikey: key },
      storageKey: `sb-${base.hostname.split('.')[0]}-auth-token`,
      autoRefreshToken: true,
      persistSession: true,
      detectSessionInUrl: true,
      flowType: 'implicit',
    }),
  };
};

export const supabase = E2E_SESSION
  ? sessionClient(E2E_SESSION)
  : isSupabaseConfigured
    ? authOnlyClient(supabaseUrl, supabaseAnonKey)
    : sessionClient(null);
