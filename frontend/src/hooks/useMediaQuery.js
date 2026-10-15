import { useEffect, useState } from 'react';

const matches = (query) => (typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  ? window.matchMedia(query).matches
  : false);

/** Live result of a CSS media query (false where matchMedia is unavailable). */
const useMediaQuery = (query) => {
  const [value, setValue] = useState(() => matches(query));
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const list = window.matchMedia(query);
    const onChange = () => setValue(list.matches);
    onChange();
    list.addEventListener?.('change', onChange);
    return () => list.removeEventListener?.('change', onChange);
  }, [query]);
  return value;
};

export default useMediaQuery;
