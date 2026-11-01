import { History, MessageSquareText, Pencil, SquarePen, Trash2 } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { cleanTitle, conversationDate, conversationSummary, titleProblem } from '../../lib/conversations';
import useStore from '../../store/useStore';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import { Popover, PopoverPopup, PopoverTrigger } from '../ui/Popover';
import { toast } from '../ui/Toast';

/** How long Undo stays offered after a conversation is deleted. */
const UNDO_MS = 10_000;

const fullDate = (iso) => {
  const date = new Date(iso || '');
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
};

/** Inline title editor: Enter saves, Cancel (or Escape, which also closes the list) keeps the old title. */
const RenameRow = ({ item, onDone }) => {
  const renameConversation = useStore((state) => state.renameConversation);
  const inputRef = useRef(null);
  const [title, setTitle] = useState(item.title || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const errorId = useId();

  useEffect(() => {
    inputRef.current?.select();
  }, []);

  const submit = async (event) => {
    event.preventDefault();
    const problem = titleProblem(title);
    if (problem) {
      setError(problem);
      return;
    }
    if (cleanTitle(title) === item.title) {
      onDone();
      return;
    }
    setBusy(true);
    setError('');
    try {
      await renameConversation(item.id, title);
      onDone();
    } catch (renameError) {
      setError(errorMessage(renameError, 'Could not rename this conversation.'));
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-1.5 rounded-lg bg-surface-2 px-2.5 py-2">
      <label className="sr-only" htmlFor={`${errorId}-input`}>Conversation title</label>
      <input
        id={`${errorId}-input`}
        ref={inputRef}
        value={title}
        onChange={(event) => { setTitle(event.target.value); setError(''); }}
        maxLength={200}
        aria-invalid={error ? 'true' : undefined}
        aria-describedby={error ? errorId : undefined}
        className="h-8 w-full rounded-md border border-line bg-surface px-2 text-[13px] text-ink outline-none focus:border-accent"
      />
      {error && <p id={errorId} className="m-0 text-[12px] text-danger" role="alert">{error}</p>}
      <div className="flex justify-end gap-1.5">
        <Button variant="ghost" size="xs" onClick={onDone} disabled={busy}>Cancel</Button>
        <Button variant="primary" size="xs" type="submit" loading={busy}>Save</Button>
      </div>
    </form>
  );
};

/**
 * The selected repository's saved conversations: a "History" button in the
 * chat header that opens the list (title, last activity, message and
 * proposal counts). Choosing one opens it; each can be renamed or deleted.
 * Deleting asks first and offers Undo, because nothing is erased.
 */
const ConversationList = ({ repoName, onNewChat, onOpen }) => {
  const list = useStore((state) => state.conversationList);
  const conversation = useStore((state) => state.conversation);
  const hasMessages = useStore((state) => state.messages.length > 0);
  const isQuerying = useStore((state) => state.isQuerying);
  const loadConversations = useStore((state) => state.loadConversations);
  const selectConversation = useStore((state) => state.selectConversation);
  const deleteConversation = useStore((state) => state.deleteConversation);
  const restoreConversation = useStore((state) => state.restoreConversation);
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [pendingDelete, setPendingDelete] = useState(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const titleId = useId();
  const items = list.repoName === repoName ? list.items : [];
  const loaded = list.repoName === repoName && list.loaded;

  const onOpenChange = (next) => {
    setOpen(next);
    setEditingId(null);
    // Refreshed on every open: answers and other tabs change the list.
    if (next) void loadConversations();
  };

  const choose = (id) => {
    setOpen(false);
    onOpen?.();
    void selectConversation(id);
  };

  const askDelete = (item) => {
    setDeleteError('');
    setPendingDelete(item);
    setOpen(false);
  };

  const undo = async (item, { wasOpen, repoName: deletedFrom }) => {
    try {
      await restoreConversation(item.id, { repoName: deletedFrom, reopen: wasOpen });
      toast.success('Conversation restored', { description: item.title });
    } catch (error) {
      toast.error('Could not restore the conversation', { description: errorMessage(error, 'Please try again.') });
    }
  };

  const confirmDelete = async () => {
    const item = pendingDelete;
    setDeleting(true);
    setDeleteError('');
    try {
      const result = await deleteConversation(item.id);
      setPendingDelete(null);
      toast({
        title: 'Conversation deleted',
        description: item.title,
        duration: UNDO_MS,
        action: { label: 'Undo', onClick: () => { void undo(item, result); } },
      });
    } catch (error) {
      setDeleteError(errorMessage(error, 'Could not delete this conversation.'));
    } finally {
      setDeleting(false);
    }
  };

  let body;
  if (!loaded && list.loading) {
    body = (
      <div className="space-y-1 p-1" aria-busy="true">
        <span className="sr-only" role="status">Loading conversations</span>
        {[0, 1, 2].map((index) => <div key={index} className="skeleton h-12 rounded-lg" aria-hidden="true" />)}
      </div>
    );
  } else if (list.error && !items.length) {
    body = (
      <div className="flex flex-col items-start gap-2 px-2.5 py-3 text-[12.5px] text-ink-2" role="alert">
        <p className="m-0">{list.error}</p>
        <Button variant="secondary" size="xs" onClick={() => loadConversations()}>Try again</Button>
      </div>
    );
  } else if (!items.length) {
    body = (
      <p className="m-0 px-2.5 py-3 text-[12.5px] leading-relaxed text-ink-3">
        No saved conversations yet. Each question you ask is saved in a conversation you can come back to.
      </p>
    );
  } else {
    body = (
      <ul className="m-0 flex list-none flex-col gap-0.5 p-0" aria-labelledby={titleId}>
        {items.map((item) => {
          const active = item.id === conversation?.id;
          if (editingId === item.id) {
            return <li key={item.id}><RenameRow item={item} onDone={() => setEditingId(null)} /></li>;
          }
          return (
            <li key={item.id} className="group relative">
              <button
                type="button"
                onClick={() => choose(item.id)}
                disabled={isQuerying && !active}
                aria-current={active ? 'true' : undefined}
                className="flex w-full min-w-0 flex-col items-start gap-0.5 rounded-lg px-2.5 py-2 pr-16 text-left transition-colors enabled:hover:bg-surface-2 disabled:opacity-50 aria-[current=true]:bg-surface-2"
              >
                <span className="line-clamp-2 break-words text-[13px] font-medium leading-snug text-ink">
                  {item.title || 'Untitled conversation'}
                  {active && <span className="sr-only"> (open)</span>}
                </span>
                <span className="text-[11.5px] leading-4 text-ink-3">
                  <time dateTime={item.updated_at} title={fullDate(item.updated_at)}>{conversationDate(item.updated_at)}</time>
                  {' · '}
                  {conversationSummary(item)}
                </span>
              </button>
              <div className="absolute right-1 top-1.5 flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100">
                <Button variant="link" size="icon-sm" aria-label={`Rename “${item.title}”`} title="Rename" onClick={() => setEditingId(item.id)}>
                  <Pencil className="size-3.5" aria-hidden="true" />
                </Button>
                <Button
                  variant="link"
                  size="icon-sm"
                  aria-label={`Delete “${item.title}”`}
                  title="Delete"
                  disabled={isQuerying && active}
                  onClick={() => askDelete(item)}
                  className="hover:text-danger"
                >
                  <Trash2 className="size-3.5" aria-hidden="true" />
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
    );
  }

  // "with its 6 messages and 1 proposal"
  const deletingSummary = pendingDelete ? conversationSummary(pendingDelete).replace(' · ', ' and ') : '';

  return (
    <>
      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger
          render={<Button variant="ghost" size="sm" className="max-sm:w-9 max-sm:px-0" />}
          aria-label="Conversation history"
          title="Conversation history"
        >
          <History className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="max-sm:sr-only">History</span>
        </PopoverTrigger>
        <PopoverPopup align="end" aria-labelledby={titleId} className="flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col overflow-hidden p-0">
          <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
            <p id={titleId} className="m-0 text-[13px] font-semibold text-ink">Conversations</p>
            <Button
              variant="ghost"
              size="xs"
              icon={SquarePen}
              disabled={isQuerying || (!conversation && !hasMessages)}
              onClick={() => { setOpen(false); onNewChat(); }}
            >
              New chat
            </Button>
          </div>
          {isQuerying && (
            <p className="m-0 flex items-start gap-1.5 border-b border-line px-3 py-2 text-[12px] leading-relaxed text-ink-3">
              <MessageSquareText className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              Wait for the answer to finish, or stop it, to switch conversations.
            </p>
          )}
          <div className="scroll-thin max-h-[min(26rem,calc(var(--available-height,26rem)-3rem))] overflow-y-auto p-1">
            {body}
            {list.error && items.length > 0 && <p className="m-0 px-2.5 py-2 text-[12px] text-danger" role="alert">{list.error}</p>}
            {loaded && list.hasMore && (
              <div className="flex justify-center p-1">
                <Button variant="ghost" size="xs" loading={list.loading} onClick={() => loadConversations({ more: true })}>Show older conversations</Button>
              </div>
            )}
          </div>
        </PopoverPopup>
      </Popover>

      <Modal
        open={Boolean(pendingDelete)}
        onClose={() => setPendingDelete(null)}
        busy={deleting}
        size="sm"
        title="Delete this conversation?"
        description={pendingDelete?.title}
        footer={(
          <>
            <Button variant="ghost" size="sm" onClick={() => setPendingDelete(null)} disabled={deleting}>Cancel</Button>
            <Button variant="danger" size="sm" loading={deleting} onClick={confirmDelete}>Delete</Button>
          </>
        )}
      >
        <div className="space-y-2 text-[13px] leading-relaxed text-ink-2">
          <p className="m-0">
            It leaves your history{deletingSummary ? `, with its ${deletingSummary}` : ''}. Your other conversations are not affected.
          </p>
          <p className="m-0 text-ink-3">You can undo this from the notification that appears next.</p>
          {deleteError && <p className="m-0 text-danger" role="alert">{deleteError}</p>}
        </div>
      </Modal>
    </>
  );
};

export default ConversationList;
