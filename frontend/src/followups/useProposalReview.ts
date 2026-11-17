import { useWorkspace } from '../workspace';
import { useQueryClient } from '@tanstack/react-query';
import { refreshAfterProposalChange } from '../queries';
import { useRef, useState } from 'react';
import { type Citation, type FollowUpProposal, type SectionName, type Session } from '../types';

import { describe } from './shared';
export function useProposalReview(proposal: FollowUpProposal, refresh: () => Promise<unknown>) {
  const { api, identity, run, notify } = useWorkspace();
  const cache = useQueryClient();
  const [selection, setSelection] = useState<Partial<Record<SectionName, 'merge' | 'replace'>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [source, setSource] = useState<{
    session: string;
    turn: number;
    text: string;
    error: string;
    loading: boolean;
  } | null>(null);
  const sourceRequest = useRef(0);
  async function openSource(citation: Citation) {
    if (citation.kind !== 'interview_turn') return;
    const request = ++sourceRequest.current;
    const initial = {
      session: citation.session_id,
      turn: citation.turn_index,
      text: '',
      error: '',
      loading: true,
    };
    setSource(initial);
    try {
      const transcript = await api.request<Session>(`/sessions/${citation.session_id}`);
      const turn = transcript.turns.find(item => item.ordinal === citation.turn_index);
      if (sourceRequest.current === request)
        setSource({
          ...initial,
          loading: false,
          text: turn
            ? turn.confidential
              ? '[confidential]'
              : turn.text
            : 'This source turn is unavailable.',
        });
    } catch (caught) {
      if (sourceRequest.current === request)
        setSource({ ...initial, loading: false, error: describe(caught) });
    }
  }
  async function apply() {
    if (!Object.keys(selection).length) return;
    setBusy(true);
    setError('');
    try {
      await api.request(`/follow-up-proposals/${proposal.id}/apply`, 'POST', {
        applied_by: identity.name,
        sections: selection,
        expected_revision: proposal.revision,
        expected_version: proposal.base_version,
        ...(proposal.maintenance_revision == null
          ? {}
          : { expected_maintenance_revision: proposal.maintenance_revision }),
      });
      await refresh();
      await refreshAfterProposalChange(cache, proposal.entry_id);
      notify('Selected sections applied. Review and approve the updated draft.');
    } catch (caught) {
      setError(describe(caught));
      await refresh();
    } finally {
      setBusy(false);
      setReplaceOpen(false);
    }
  }
  async function rebase() {
    setBusy(true);
    setError('');
    setSelection({});
    try {
      await api.request(`/follow-up-proposals/${proposal.id}/refresh`, 'POST', {
        refreshed_by: identity.name,
        expected_revision: proposal.revision,
        expected_version: proposal.entry_version,
      });
      await refresh();
      notify('Proposal refreshed. Review the comparison again before selecting sections.');
    } catch (caught) {
      setError(describe(caught));
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  return {
    run,
    selection,
    setSelection,
    busy,
    error,
    replaceOpen,
    setReplaceOpen,
    source,
    setSource,
    sourceRequest,
    openSource,
    apply,
    rebase,
  };
}
