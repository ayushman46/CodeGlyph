import { cn } from '../../lib/cn';

/**
 * Text with a highlight sweeping across it (Honest UI TextShimmer): a
 * 110deg gradient from muted ink (ink-3) through full ink and back, clipped
 * to the glyphs and moved once per `duration` seconds. Both ends of the
 * sweep keep WCAG AA on canvas and surface-1..3 in either theme (light
 * ink-3 is 4.54:1 at worst, dark ink-3 5.18:1; ink is higher).
 *
 * Under prefers-reduced-motion it is plain text in the inherited color; in
 * forced-colors mode it is plain CanvasText. Styles: `.ci-text-shimmer` in
 * index.css (unlayered, so a text-* utility cannot break the clip).
 *
 * @param {object} props
 * @param {import('react').ReactNode} props.children
 * @param {number} [props.duration=1.6] seconds per sweep
 * @param {string} [props.className]
 * @param {keyof JSX.IntrinsicElements} [props.as='span']
 */
export function TextShimmer({ children, duration = 1.6, className, as: Component = 'span', style, ...props }) {
  return (
    <Component
      data-slot="text-shimmer"
      className={cn('ci-text-shimmer', className)}
      style={{ '--ci-shimmer-duration': `${duration}s`, ...style }}
      {...props}
    >
      {children}
    </Component>
  );
}

export default TextShimmer;
