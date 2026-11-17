import { useWorkspace } from '../workspace';
import { queryKeys, refreshAfterEntryChange, refreshAfterEntryMaintenance } from '../queries';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation, useParams } from 'react-router-dom';
import { dayAfter, expected } from './reviewMetadata';
import {
  sections,
  type Citation,
  type Entry,
  type SectionName,
  type Session,
  type Turn,
} from '../types';

import { errorMessage } from './messages';
export function useEntryReview() {
  const { entryId = '' } = useParams();
  const location = useLocation();
  const { api, run, identity, notify } = useWorkspace();
  const cache = useQueryClient();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [reviewBy, setReviewBy] = useState(dayAfter(90));
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState('');
  const [editing, setEditing] = useState<SectionName | null>(null);
  const [claims, setClaims] = useState<string[]>([]);
  const [openQuestion, setOpenQuestion] = useState('');
  const [editorError, setEditorError] = useState('');
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceText, setSourceText] = useState('');
  const [sourceLabel, setSourceLabel] = useState('');
  const [sourceError, setSourceError] = useState('');
  const sourceRequest = useRef(0);
  const currentEntry = useRef(entryId);
  currentEntry.current = entryId;
  useEffect(() => {
    sourceRequest.current += 1;
    setError('');
    setBusy(false);
    setHistoryOpen(false);
    setRejectOpen(false);
    setReason('');
    setReasonError('');
    setEditing(null);
    setClaims([]);
    setOpenQuestion('');
    setEditorError('');
    setSourceOpen(false);
    setSourceText('');
    setSourceError('');
  }, [entryId]);
  const entryQuery = useQuery({
    queryKey: queryKeys.entry(entryId),
    queryFn: () => api.request<Entry>(`/entries/${entryId}`),
  });
  const entry = entryQuery.data;
  useEffect(() => {
    if (
      !entry ||
      !/^#section-(trigger|diagnosis|steps|rollback|contacts|pitfalls|rationale)$/.test(
        location.hash,
      )
    )
      return;
    const section = document.getElementById(location.hash.slice(1));
    section?.scrollIntoView({ block: 'start' });
    section?.focus({ preventScroll: true });
  }, [entry?.id, location.hash]);
  // Reader responses omit management fields, even when opened from cross-handover search.
  const manager = Boolean(run.can_manage && entry?.decisions);
  const canReview = manager && entry?.can_review !== false;

  function editSection(name: SectionName) {
    if (!entry || !canReview) return;
    setEditing(name);
    setClaims(entry[name].claims.map(claim => claim.text));
    setOpenQuestion(entry[name].open_question ?? '');
    setEditorError('');
  }
  async function update(path: string, method: string, body: object, success: string) {
    setBusy(true);
    setError('');
    try {
      const updated = await api.request<Entry>(path, method, {
        ...(entry ? expected(entry) : {}),
        ...body,
      });
      cache.setQueryData(queryKeys.entry(entryId), updated);
      await refreshAfterEntryChange(cache);
      if (currentEntry.current === entryId) notify(success);
      return true;
    } catch (caught) {
      if (currentEntry.current === entryId) setError(errorMessage(caught));
      return false;
    } finally {
      if (currentEntry.current === entryId) setBusy(false);
    }
  }
  async function saveSection(event: FormEvent) {
    event.preventDefault();
    if (!entry || !editing) return;
    const statements = claims.map(claim => claim.trim()).filter(Boolean);
    if (!statements.length && !openQuestion.trim()) {
      setEditorError('Keep at least one statement or an open question for this section.');
      return;
    }
    const ok = await update(
      `/entries/${entry.id}`,
      'PATCH',
      {
        edited_by: identity.name,
        sections: { [editing]: { claims: statements, open_question: openQuestion.trim() || null } },
      },
      'Edits saved with your attribution. The entry is a draft until you approve it.',
    );
    if (ok) setEditing(null);
  }
  async function approve() {
    if (!entry) return;
    if (!reviewBy || reviewBy < dayAfter(1) || reviewBy > dayAfter(365)) {
      setError('Choose a review date from tomorrow through 365 days ahead.');
      return;
    }
    const missing = sections.filter(
      name => !entry[name].claims.length && !entry[name].open_question,
    );
    const uncited = sections.filter(name =>
      entry[name].claims.some(claim => claim.citations.length === 0),
    );
    if (missing.length || uncited.length) {
      setError(
        [
          missing.length ? `Add a statement or open question for: ${missing.join(', ')}.` : '',
          uncited.length ? `Every statement needs a source. Check: ${uncited.join(', ')}.` : '',
        ]
          .filter(Boolean)
          .join(' '),
      );
      return;
    }
    await update(
      `/entries/${entry.id}/approve`,
      'POST',
      { approved_by: identity.name, review_by: reviewBy },
      'Entry approved. It is now visible to permitted readers.',
    );
  }
  async function reject(event: FormEvent) {
    event.preventDefault();
    if (!entry || !reason.trim()) {
      setReasonError('Add a short reason so the decision makes sense later.');
      return;
    }
    if (
      await update(
        `/entries/${entry.id}/reject`,
        'POST',
        { rejected_by: identity.name, reason: reason.trim() },
        'Entry rejected. It is kept in review history and excluded from reader access.',
      )
    ) {
      setRejectOpen(false);
      setReason('');
    }
  }
  async function openSource(citation: Citation) {
    if (!manager || citation.kind !== 'interview_turn') return;
    const requestId = ++sourceRequest.current;
    setSourceOpen(true);
    setSourceLoading(true);
    setSourceError('');
    setSourceText('');
    setSourceLabel(`${citation.author_name ?? 'Expert answer'} · turn ${citation.turn_index}`);
    try {
      const session = await api.request<Session>(`/sessions/${citation.session_id}`);
      const turn = session.turns.find((item: Turn) => item.ordinal === citation.turn_index);
      if (sourceRequest.current === requestId)
        setSourceText(
          turn
            ? turn.confidential
              ? '[confidential]'
              : turn.text
            : 'This source turn is unavailable.',
        );
    } catch (caught) {
      if (sourceRequest.current === requestId) setSourceError(errorMessage(caught));
    } finally {
      if (sourceRequest.current === requestId) setSourceLoading(false);
    }
  }
  async function download() {
    setBusy(true);
    setError('');
    try {
      await api.download(`/entries/${entryId}/markdown`, `eki-entry-${entryId}.md`);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  const maintenanceChanged = () =>
    Promise.all([entryQuery.refetch(), refreshAfterEntryMaintenance(cache)]);
  return {
    run,
    identity,
    entryQuery,
    entry,
    manager,
    canReview,
    busy,
    error,
    historyOpen,
    setHistoryOpen,
    reviewBy,
    setReviewBy,
    rejectOpen,
    setRejectOpen,
    reason,
    setReason,
    reasonError,
    setReasonError,
    editing,
    setEditing,
    claims,
    setClaims,
    openQuestion,
    setOpenQuestion,
    editorError,
    sourceOpen,
    setSourceOpen,
    sourceLoading,
    sourceText,
    sourceLabel,
    sourceError,
    editSection,
    saveSection,
    approve,
    reject,
    openSource,
    download,
    maintenanceChanged,
  };
}
