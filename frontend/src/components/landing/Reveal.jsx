import { motion, useReducedMotion } from 'framer-motion';

export const EASE_OUT = [0.2, 0.7, 0.2, 1];

/**
 * Fades and lifts its children into place the first time they scroll into
 * view. Visitors who prefer reduced motion get the content immediately, with
 * no transform or opacity animation at all.
 */
const Reveal = ({ as = 'div', delay = 0, y = 18, amount = 0.15, className = '', children, ...props }) => {
  const reduceMotion = useReducedMotion();

  if (reduceMotion) {
    const Tag = as;
    return <Tag className={className} {...props}>{children}</Tag>;
  }

  const MotionTag = motion[as] || motion.div;
  return (
    <MotionTag
      className={className}
      initial={{ opacity: 0, y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, amount }}
      transition={{ duration: 0.5, ease: EASE_OUT, delay }}
      {...props}
    >
      {children}
    </MotionTag>
  );
};

export default Reveal;
