import { useEffect, useState } from 'react';

/**
 * False for `delay` ms, then true. Use it to hold back a spinner or skeleton
 * so fast loads never flash a loading state:
 *   const show = useDelayedFlag(200); return show ? <Spinner /> : null;
 */
const useDelayedFlag = (delay = 200) => {
  const [ready, setReady] = useState(delay <= 0);
  useEffect(() => {
    if (delay <= 0) return undefined;
    const timer = window.setTimeout(() => setReady(true), delay);
    return () => window.clearTimeout(timer);
  }, [delay]);
  return ready;
};

export default useDelayedFlag;
