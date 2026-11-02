import { useEffect, useState } from 'react';
import { getStatus } from '../../api/client';

/**
 * For a repository name that is not in the workspace: asks the server
 * whether it was shared with you and its owner removed you (a 404 with
 * X-Error-Code share_removed). Returns null while checking, then
 * { removed, detail }.
 */
const useShareRemoved = (repoName) => {
  const [result, setResult] = useState(null);
  useEffect(() => {
    if (!repoName) return undefined;
    let active = true;
    setResult(null);
    getStatus(repoName)
      .then(() => { if (active) setResult({ removed: false, detail: '' }); })
      .catch((error) => {
        if (!active) return;
        const removed = error?.response?.headers?.['x-error-code'] === 'share_removed';
        setResult({ removed, detail: removed ? String(error.response.data?.detail || '') : '' });
      });
    return () => { active = false; };
  }, [repoName]);
  return result;
};

export default useShareRemoved;
