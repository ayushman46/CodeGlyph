import { FolderGit2, UserX } from 'lucide-react';
import Button from '../ui/Button';
import useShareRemoved from './useShareRemoved';

/**
 * A repository name that is not in the workspace. When it had been shared
 * with you and the owner removed you, say so; otherwise show `children`
 * (the usual "Repository not found").
 */
const MissingRepository = ({ name, children }) => {
  const check = useShareRemoved(name);
  if (!check) return <div className="h-full" aria-busy="true"><span className="sr-only" role="status">Checking {name}</span></div>;
  if (!check.removed) return children;
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="w-full max-w-md text-center" role="alert">
        <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2 text-ink-3"><UserX className="h-5 w-5" aria-hidden="true" /></div>
        <h1 className="mt-4 text-base font-semibold text-ink">{check.detail || 'This repository is no longer shared with you.'}</h1>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-3">
          Its owner removed your access to <span className="font-mono text-ink-2">{name}</span>. Ask them for a new invite link if you still need it.
        </p>
        <Button className="mt-5" variant="primary" size="sm" icon={FolderGit2} to="/app">Go to your repositories</Button>
      </div>
    </div>
  );
};

export default MissingRepository;
