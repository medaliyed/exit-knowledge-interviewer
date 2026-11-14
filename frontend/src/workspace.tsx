import { createContext, useContext } from 'react';
import type { ApiClient } from './api';
import type { Identity, Run } from './types';

export interface Workspace {
  api: ApiClient;
  identity: Identity;
  run: Run;
  notify: (message: string, error?: boolean) => void;
  drafts: Map<string, string>;
}
export const WorkspaceContext = createContext<Workspace | null>(null);
export function useWorkspace(): Workspace {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error('Open a handover to continue.');
  return value;
}
