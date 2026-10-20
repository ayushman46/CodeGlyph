/**
 * Joins class names, clsx-style: strings and numbers are kept, arrays are
 * flattened, objects contribute the keys whose values are truthy, and every
 * falsy value is skipped. There is no Tailwind conflict resolution: when two
 * utilities set the same property, the one later in Tailwind's generated CSS
 * wins, not the one later in this list. Primitives that expect overrides keep
 * their defaults in `@layer components` (index.css) so any utility beats them.
 *
 * @param {...(string|number|null|undefined|false|Record<string, unknown>|Array<unknown>)} inputs
 * @returns {string}
 */
export function cn(...inputs) {
  const out = [];
  const visit = (value) => {
    if (!value) return;
    if (typeof value === 'string' || typeof value === 'number') {
      out.push(String(value));
    } else if (Array.isArray(value)) {
      value.forEach(visit);
    } else if (typeof value === 'object') {
      for (const key of Object.keys(value)) {
        if (value[key]) out.push(key);
      }
    }
  };
  inputs.forEach(visit);
  return out.join(' ').replace(/\s+/g, ' ').trim();
}

export default cn;
