import { Navigate, useLocation } from 'react-router-dom';
import useStore from '../store/useStore';

/**
 * App renders routes only after the session check settles, so this never
 * flashes protected content. Signed-out visitors go to the home page; the
 * full path they asked for (search and hash included) travels in the history
 * state, and the sign-in buttons send them back to it after Google.
 */
const RequireAuth = ({ children }) => {
  const user = useStore((state) => state.user);
  const location = useLocation();
  if (user) return children;
  const from = `${location.pathname}${location.search}${location.hash}`;
  return <Navigate to="/" replace state={{ from }} />;
};

export default RequireAuth;
