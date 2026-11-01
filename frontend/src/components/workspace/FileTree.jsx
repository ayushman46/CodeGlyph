import { ChevronRight, FileCode2, FileText, Folder, FolderOpen, RotateCcw } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage, listRepositoryFiles } from '../../api/client';
import { formatCount } from '../../lib/format';

const DOCUMENT_FILE = /\.(md|mdx|markdown|txt|rst|adoc)$/i;
// Very large folders list this many files until "Show all" is chosen.
const FILES_SHOWN = 200;

const indent = (depth) => ({ paddingLeft: `${depth * 16 + 6}px` });

/**
 * One folder's contents: sub-folders (source first, then tests, examples,
 * docs, then dot-folders, as the API orders them) and its files. Folders
 * load when first expanded.
 */
const FolderContents = ({ dir, depth, folders, expanded, onToggle, onRetry, onOpenFile }) => {
  const [all, setAll] = useState(false);
  const entry = folders[dir];
  if (!entry || entry.loading) {
    return (
      <li className="py-1 text-[12px] text-ink-3" style={indent(depth)} role="status">
        <span className="skeleton inline-block h-3.5 w-40 align-middle" aria-hidden="true" /><span className="sr-only">Loading {dir || 'the repository'}</span>
      </li>
    );
  }
  if (entry.error) {
    return (
      <li className="flex items-center gap-2 py-1 text-[12px] text-danger" style={indent(depth)} role="alert">
        {entry.error}
        <button type="button" onClick={() => onRetry(dir)} className="inline-flex items-center gap-1 font-medium text-ink-2 hover:text-ink">
          <RotateCcw className="h-3 w-3" aria-hidden="true" /> Try again
        </button>
      </li>
    );
  }
  const files = all ? entry.data.files : entry.data.files.slice(0, FILES_SHOWN);
  return (
    <>
      {entry.data.directories.map((folder) => {
        const open = expanded.has(folder.path);
        return (
          <li key={folder.path}>
            <button
              type="button"
              onClick={() => onToggle(folder.path)}
              aria-expanded={open}
              className="flex w-full items-center gap-1.5 rounded-lg py-1 pr-2 text-left text-[13px] transition-colors hover:bg-surface-2"
              style={indent(depth)}
            >
              <ChevronRight className={`h-3.5 w-3.5 shrink-0 text-ink-3 transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
              {open ? <FolderOpen className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" /> : <Folder className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />}
              <span className="truncate font-mono text-[12.5px] text-ink-2">{folder.name}/</span>
              <span className="ml-auto shrink-0 font-mono text-[11px] text-ink-3"><span className="sr-only">, </span>{formatCount(folder.files)}<span className="sr-only">{folder.files === 1 ? ' file' : ' files'}</span></span>
            </button>
            {open && (
              <ul>
                <FolderContents dir={folder.path} depth={depth + 1} folders={folders} expanded={expanded} onToggle={onToggle} onRetry={onRetry} onOpenFile={onOpenFile} />
              </ul>
            )}
          </li>
        );
      })}
      {files.map((file) => {
        const Icon = DOCUMENT_FILE.test(file.name) ? FileText : FileCode2;
        return (
          <li key={file.path}>
            <button
              type="button"
              onClick={() => onOpenFile(file.path)}
              className="flex w-full items-center gap-1.5 rounded-lg py-1 pr-2 text-left transition-colors hover:bg-surface-2 hover:text-ink"
              style={indent(depth)}
              title={`Open ${file.path}`}
            >
              <span className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <Icon className="h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden="true" />
              <span className="truncate font-mono text-[12.5px] text-ink-2">{file.name}</span>
            </button>
          </li>
        );
      })}
      {!all && entry.data.files.length > FILES_SHOWN && (
        <li style={indent(depth)}>
          <button type="button" onClick={() => setAll(true)} className="ml-5 py-1 text-[12px] font-medium text-accent-strong hover:underline">
            Show all {formatCount(entry.data.files.length)} files
          </button>
        </li>
      )}
      {depth === 0 && !entry.data.directories.length && !entry.data.files.length && (
        <li className="py-1 text-[13px] text-ink-3">No indexed files yet.</li>
      )}
    </>
  );
};

/**
 * The repository's indexed files as a browsable tree. Folders expand in
 * place; choosing a file opens it in the viewer.
 */
const FileTree = ({ repoName, onOpenFile, className = '' }) => {
  const [folders, setFolders] = useState({});
  const [expanded, setExpanded] = useState(() => new Set());
  const repoRef = useRef(repoName);
  repoRef.current = repoName;

  const load = useCallback(async (dir) => {
    const forRepo = repoName;
    setFolders((current) => ({ ...current, [dir]: { loading: true } }));
    try {
      const response = await listRepositoryFiles(forRepo, { dir });
      if (repoRef.current !== forRepo) return;
      const data = { directories: response.data?.directories || [], files: response.data?.files || [] };
      setFolders((current) => ({ ...current, [dir]: { data } }));
    } catch (error) {
      if (repoRef.current !== forRepo) return;
      setFolders((current) => ({ ...current, [dir]: { error: errorMessage(error, 'Could not list these files.') } }));
    }
  }, [repoName]);

  useEffect(() => {
    setFolders({});
    setExpanded(new Set());
    void load('');
  }, [load]);

  const toggle = (dir) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(dir)) next.delete(dir);
      else next.add(dir);
      return next;
    });
    if (!folders[dir] || folders[dir].error) void load(dir);
  };

  return (
    <ul className={className} aria-label="Indexed files">
      <FolderContents dir="" depth={0} folders={folders} expanded={expanded} onToggle={toggle} onRetry={load} onOpenFile={onOpenFile} />
    </ul>
  );
};

export default FileTree;
