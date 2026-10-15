/**
 * Placeholder block for content that is on its way. Prefer skeletons when
 * the shape of the result is known (lists, cards, a page); keep spinners for
 * short, shapeless waits (a button action, a single value). Skeletons are
 * hidden from assistive tech: wrap a group in an element with
 * role="status" and an aria-label such as "Loading repositories".
 */
const Skeleton = ({ className = '', as: Tag = 'div', ...props }) => (
  <Tag aria-hidden="true" className={`skeleton ${className}`} {...props} />
);

/** A few lines of text-shaped skeleton; the last line is shorter. */
export const SkeletonText = ({ lines = 3, className = '', lineClassName = 'h-3' }) => (
  <div className={`space-y-2.5 ${className}`} aria-hidden="true">
    {Array.from({ length: lines }, (_, index) => (
      <div key={index} className={`skeleton ${lineClassName}`} style={{ width: index === lines - 1 && lines > 1 ? '62%' : '100%' }} />
    ))}
  </div>
);

export default Skeleton;
