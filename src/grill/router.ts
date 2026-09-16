export interface RouteState {
  mode: 'log' | 'grill';
  token?: string;
}

export function parseRoute(path: string): RouteState {
  if (path.startsWith('/grill/s/')) {
    const token = path.slice('/grill/s/'.length).split('/')[0].split('?')[0].trim();
    return { mode: 'grill', token: token || undefined };
  }
  if (path === '/grill' || path.startsWith('/grill/')) {
    return { mode: 'grill' };
  }
  return { mode: 'log' };
}
