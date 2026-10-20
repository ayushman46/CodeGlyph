import { forwardRef } from 'react';
import { Link } from 'react-router-dom';
import Spinner from './Spinner';

/*
 * State model shared by every variant:
 * - hover / active only apply while the button is enabled (`not-disabled:`),
 *   so a disabled button never pretends to respond;
 * - pressed/expanded toggles (aria-pressed, aria-expanded) read as "on";
 * - disabled, loading (aria-busy) and the 1px press live in `.ci-btn`
 *   (index.css) so they also work for callers of `buttonClass`;
 * - focus uses the global ring from index.css.
 */
const VARIANTS = {
  primary: 'bg-accent text-accent-ink shadow-sm not-disabled:hover:bg-accent-strong not-disabled:active:bg-accent-strong not-disabled:active:shadow-none',
  secondary: 'bg-surface text-ink border border-line not-disabled:hover:border-line-strong not-disabled:hover:bg-surface-2 not-disabled:active:bg-surface-3 aria-expanded:border-line-strong aria-expanded:bg-surface-2 aria-pressed:border-line-strong aria-pressed:bg-surface-2',
  ghost: 'text-ink-2 not-disabled:hover:bg-surface-2 not-disabled:hover:text-ink not-disabled:active:bg-surface-3 aria-expanded:bg-surface-2 aria-expanded:text-ink aria-pressed:bg-surface-2 aria-pressed:text-ink',
  dark: 'bg-ink text-canvas not-disabled:hover:bg-ink/82 not-disabled:active:bg-ink/74',
  danger: 'bg-danger text-danger-ink not-disabled:hover:bg-danger-strong not-disabled:active:bg-danger-strong',
  'danger-ghost': 'text-danger not-disabled:hover:bg-danger-soft not-disabled:active:bg-danger/15',
  // Honest UI "link": a quiet icon or text button with no fill until hovered
  // or pressed. Child icons without a size-* class default to 16px.
  link: "text-ink not-disabled:hover:bg-surface-2 not-disabled:active:bg-surface-3 aria-expanded:bg-surface-2 data-popup-open:bg-surface-2 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
};

// Touch screens get a little more height on the small sizes (WCAG 2.5.8 is
// met everywhere; this is about comfortable thumbs, ~36-40px).
const SIZES = {
  xs: 'h-7 px-2.5 text-xs gap-1.5 rounded-lg pointer-coarse:h-8',
  sm: 'h-8 px-3 text-[13px] gap-1.5 rounded-lg pointer-coarse:h-9',
  md: 'h-10 px-4 text-sm gap-2 rounded-xl',
  lg: 'h-12 px-5 text-[15px] gap-2 rounded-xl',
  icon: 'h-9 w-9 rounded-lg justify-center pointer-coarse:h-10 pointer-coarse:w-10',
  'icon-sm': 'h-7 w-7 rounded-md justify-center pointer-coarse:h-9 pointer-coarse:w-9',
};

export const buttonClass = (variant = 'secondary', size = 'md', extra = '') => [
  'ci-btn inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap select-none',
  'transition-[background-color,color,border-color,opacity,box-shadow,transform] duration-(--duration-fast) ease-(--ease-standard)',
  VARIANTS[variant] || VARIANTS.secondary,
  SIZES[size] || SIZES.md,
  extra,
].join(' ');

/**
 * Object form of `buttonClass`, matching the references'
 * `buttonVariants({ variant: 'link', size: 'icon-sm' })`.
 *
 * @param {{ variant?: string, size?: string, className?: string }} [options]
 */
export const buttonVariants = ({ variant = 'secondary', size = 'md', className = '' } = {}) => (
  buttonClass(variant, size, className)
);

const Button = forwardRef(({
  variant = 'secondary', size = 'md', loading = false, icon: Icon, iconRight: IconRight,
  to, href, className = '', children, disabled, type = 'button', ...props
}, ref) => {
  const classes = buttonClass(variant, size, className);
  const content = (
    <>
      {loading ? <Spinner className="h-4 w-4" /> : Icon ? <Icon className="h-4 w-4 shrink-0" aria-hidden="true" /> : null}
      {children}
      {IconRight && !loading ? <IconRight className="h-4 w-4 shrink-0" aria-hidden="true" /> : null}
    </>
  );
  if (to) return <Link ref={ref} to={to} className={classes} {...props}>{content}</Link>;
  if (href) return <a ref={ref} href={href} className={classes} {...props}>{content}</a>;
  return (
    <button ref={ref} type={type} className={classes} disabled={disabled || loading} aria-busy={loading || undefined} {...props}>
      {content}
    </button>
  );
});

Button.displayName = 'Button';
export default Button;
