/*
 * Saved conversations of one repository: the history list's order, counts
 * and dates, and which conversation a repository page opens with. Pure
 * functions (the store and ConversationList call them; tests in
 * conversations.test.mjs).
 */

/** The URL search parameter holding the open conversation (`?c=<id>`). */
export const CONVERSATION_PARAM = 'c';

/** Conversation ids are 32 lowercase hex characters (as the server makes them). */
export const isConversationId = (value) => /^[0-9a-f]{32}$/.test(String(value ?? ''));

/** localStorage key remembering a repository's last open conversation. */
export const rememberedConversationKey = (repoName) => `ci-conversation:${repoName}`;

/**
 * The `?c=<id>` of a location when it is that repository's page
 * (`/app/r/<name>`), else null. A link to another repository never picks
 * this one's conversation.
 */
export const conversationFromLocation = ({ pathname = '', search = '' } = {}, repoName) => {
  const match = /^\/app\/r\/([^/]+)\/?$/.exec(pathname);
  if (!match || !repoName) return null;
  let name;
  try {
    name = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  if (name !== repoName) return null;
  const id = new URLSearchParams(search).get(CONVERSATION_PARAM);
  return isConversationId(id) ? id : null;
};

const timeOf = (value) => {
  const time = Date.parse(value || '');
  return Number.isNaN(time) ? 0 : time;
};

/** Most recently active first, then by id: the server's order. */
export const sortConversations = (items) => [...items].sort((a, b) => (
  timeOf(b.updated_at) - timeOf(a.updated_at) || String(b.id).localeCompare(String(a.id))
));

/** Add or update one conversation (merging its fields), keeping the order. */
export const upsertConversation = (items, conversation) => {
  if (!conversation?.id) return items;
  const existing = items.find((item) => item.id === conversation.id);
  const rest = items.filter((item) => item.id !== conversation.id);
  return sortConversations([...rest, { ...existing, ...conversation }]);
};

/** Append an older page, ignoring conversations already listed. */
export const appendConversations = (items, page) => {
  const known = new Set(items.map((item) => item.id));
  return [...items, ...page.filter((item) => item?.id && !known.has(item.id))];
};

/**
 * The list after an answer was saved in `conversation`: two more messages
 * (the question and the answer), one more proposal when the answer has one,
 * and the conversation moves to the top.
 */
export const recordExchange = (items, conversation, { proposal = false } = {}) => {
  if (!conversation?.id) return items;
  const existing = items.find((item) => item.id === conversation.id);
  return upsertConversation(items, {
    ...conversation,
    message_count: (Number(existing?.message_count) || 0) + 2,
    proposal_count: (Number(existing?.proposal_count) || 0) + (proposal ? 1 : 0),
  });
};

const plural = (count, singular, pluralForm = `${singular}s`) => `${count} ${count === 1 ? singular : pluralForm}`;

/** "6 messages · 1 proposal" (proposals only when there are some). */
export const conversationSummary = ({ message_count: messages = 0, proposal_count: proposals = 0 } = {}) => {
  const parts = [plural(Math.max(0, Number(messages) || 0), 'message')];
  if (Number(proposals) > 0) parts.push(plural(Number(proposals), 'proposal'));
  return parts.join(' · ');
};

const startOfDay = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();

/**
 * When a conversation was last active, for the history list: "Today, 14:05",
 * "Yesterday, 09:30", "Oct 28", or "Oct 28, 2025" in an earlier year.
 */
export const conversationDate = (iso, { now = new Date(), locale } = {}) => {
  const date = new Date(iso || '');
  if (!iso || Number.isNaN(date.getTime())) return '';
  const time = new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit' }).format(date);
  const days = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
  if (days === 0) return `Today, ${time}`;
  if (days === 1) return `Yesterday, ${time}`;
  return new Intl.DateTimeFormat(locale, {
    month: 'short', day: 'numeric', ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  }).format(date);
};

/** A typed title as the server stores it: one line, single spaces. */
export const cleanTitle = (value) => String(value ?? '').split(/\s+/).filter(Boolean).join(' ');

export const TITLE_MAX = 120;

/** Why a typed title cannot be saved, or '' when it can. */
export const titleProblem = (value) => {
  const title = cleanTitle(value);
  if (!title) return 'Enter a title.';
  if (title.length > TITLE_MAX) return `Titles can be at most ${TITLE_MAX} characters.`;
  return '';
};
