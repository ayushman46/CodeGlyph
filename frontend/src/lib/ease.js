/*
 * Shared motion tokens for framer-motion (the reference components import
 * these from "@/lib/ease"). Strong custom curves: the stock ease-in/ease-out
 * feel weak next to them.
 */

/** Entrances, fades and value glides. */
export const EASE_OUT = [0.16, 1, 0.3, 1];
/** Symmetric moves between two resting positions. */
export const EASE_IN_OUT = [0.77, 0, 0.175, 1];

/** CSS string forms, for inline transitions and the Web Animations API. */
export const EASE_OUT_CSS = 'cubic-bezier(0.16, 1, 0.3, 1)';
export const EASE_IN_OUT_CSS = 'cubic-bezier(0.77, 0, 0.175, 1)';

/** Press feedback on buttons and other tappable surfaces (whileTap). */
export const SPRING_PRESS = { type: 'spring', stiffness: 500, damping: 30, mass: 0.6 };

/** Content swaps: label or icon slots trading places inside a control. */
export const SPRING_SWAP = { type: 'spring', stiffness: 460, damping: 30, mass: 0.55 };

/** Overlay panel entrances: panels and sheets summoned by a pointer. */
export const SPRING_PANEL = { type: 'spring', stiffness: 420, damping: 40, mass: 0.5 };

/** Shared-layout glides: pills, indicators and cards morphing between positions. */
export const SPRING_LAYOUT = { type: 'spring', stiffness: 360, damping: 32, mass: 0.6 };
