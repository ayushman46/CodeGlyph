import Prism from 'prismjs';
import { splitHighlightedLines } from './lines.js';
import 'prismjs/components/prism-clike';
import 'prismjs/components/prism-markup';
import 'prismjs/components/prism-css';
import 'prismjs/components/prism-javascript';
import 'prismjs/components/prism-jsx';
import 'prismjs/components/prism-typescript';
import 'prismjs/components/prism-tsx';
import 'prismjs/components/prism-python';
import 'prismjs/components/prism-go';
import 'prismjs/components/prism-rust';
import 'prismjs/components/prism-java';
import 'prismjs/components/prism-kotlin';
import 'prismjs/components/prism-ruby';
import 'prismjs/components/prism-markup-templating';
import 'prismjs/components/prism-php';
import 'prismjs/components/prism-csharp';
import 'prismjs/components/prism-swift';
import 'prismjs/components/prism-c';
import 'prismjs/components/prism-cpp';
import 'prismjs/components/prism-sql';
import 'prismjs/components/prism-bash';
import 'prismjs/components/prism-yaml';
import 'prismjs/components/prism-json';
import 'prismjs/components/prism-toml';
import 'prismjs/components/prism-markdown';
import 'prismjs/components/prism-graphql';
import 'prismjs/components/prism-scss';
import 'prismjs/components/prism-docker';

const ALIASES = {
  py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', mts: 'typescript',
  cts: 'typescript', rb: 'ruby', rs: 'rust', sh: 'bash', zsh: 'bash', shell: 'bash', yml: 'yaml', md: 'markdown',
  mdx: 'markdown', html: 'markup', xml: 'markup', vue: 'markup', svelte: 'markup', astro: 'markup', kt: 'kotlin',
  kts: 'kotlin', cs: 'csharp', h: 'c', hpp: 'cpp', cc: 'cpp', cxx: 'cpp', gql: 'graphql', dockerfile: 'docker',
  prisma: 'graphql', text: 'plain', txt: 'plain',
};

export const prismLanguage = (language) => {
  const key = String(language || '').toLowerCase();
  const resolved = ALIASES[key] || key;
  return Prism.languages[resolved] ? resolved : 'plain';
};

const escapeHtml = (value) => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** Highlight one string; very large inputs are escaped instead (fast path). */
export const highlight = (code, language) => {
  const text = String(code ?? '');
  const resolved = prismLanguage(language);
  if (resolved === 'plain' || text.length > 200_000) return escapeHtml(text);
  try {
    return Prism.highlight(text, Prism.languages[resolved], resolved);
  } catch {
    return escapeHtml(text);
  }
};

/**
 * Highlight a whole file and split it back into per-line HTML so line
 * numbers and range highlighting work (see splitHighlightedLines).
 */
export const highlightLines = (code, language) => splitHighlightedLines(highlight(code, language), code);
