/* Section intro used across the landing page: eyebrow, h2 and a short lead.
 * Centred leads are balanced so they never end on a one-word line.
 * The eyebrow uses the stronger accent so small uppercase text keeps AA
 * contrast on the light canvas (the important modifier is needed because the
 * shared .eyebrow class is unlayered CSS). */
const SectionHeading = ({ id, eyebrow, title, description, align = 'center', className = '' }) => {
  const centered = align === 'center';
  return (
    <div className={`${centered ? 'mx-auto max-w-2xl text-center' : 'max-w-xl'} ${className}`}>
      {eyebrow && <p className="eyebrow text-accent-strong!">{eyebrow}</p>}
      <h2 id={id} className="section-title mt-3 text-balance text-[1.875rem] text-ink sm:text-4xl lg:text-[2.625rem]">
        {title}
      </h2>
      {description && (
        <p className={`mt-4 text-base leading-relaxed text-ink-2 sm:text-[17px] ${centered ? 'mx-auto max-w-xl text-balance' : 'text-pretty'}`}>
          {description}
        </p>
      )}
    </div>
  );
};

export default SectionHeading;
