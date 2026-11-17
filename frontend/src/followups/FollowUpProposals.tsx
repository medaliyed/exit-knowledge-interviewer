import { queryKeys } from '../queries';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { useWorkspace } from '../workspace';
import { Badge, Button, Empty, Loading, Notice } from '../ui';
import { type EntrySummary, type FollowUpProposal, type Session } from '../types';

import { describe } from './shared';
import { Proposal } from './Proposal';
export function FollowUpProposals() {
  const { sessionId = '' } = useParams();
  const { api, run } = useWorkspace();
  const proposals = useQuery({
    queryKey: queryKeys.followUpProposals(sessionId),
    queryFn: () =>
      api.request<{ session_id: string; proposals: FollowUpProposal[] }>(
        `/sessions/${sessionId}/follow-up-proposals`,
      ),
  });
  const session = useQuery({
    queryKey: queryKeys.session(sessionId),
    queryFn: () => api.request<Session>(`/sessions/${sessionId}`),
  });
  const entries = useQuery({
    queryKey: queryKeys.entries(sessionId),
    queryFn: () => api.request<EntrySummary[]>(`/sessions/${sessionId}/entries`),
  });
  if (proposals.isPending || session.isPending || entries.isPending) return <Loading />;
  if (session.data && session.data.run_id !== run.id)
    return <Empty title="This follow-up belongs to another handover" />;
  return (
    <div className="page-content stack">
      <Link className="back-link" to={`/workspace/${run.id}/follow-ups`}>
        ← Follow-up gaps
      </Link>
      <div>
        <p className="eyebrow">Review captured knowledge</p>
        <h1>Follow-up proposals</h1>
        <p className="page-intro">
          Compare each selected section with its current entry. Applying creates a draft; only
          separate entry approval publishes it.
        </p>
      </div>
      {(proposals.error || session.error || entries.error) && (
        <Notice error>
          {describe(proposals.error ?? session.error ?? entries.error)}{' '}
          <Button
            onClick={() => {
              void proposals.refetch();
              void session.refetch();
              void entries.refetch();
            }}
          >
            Try again
          </Button>
        </Notice>
      )}
      {session.data?.usage?.partial && (
        <Notice>
          Some selected questions remain unresolved. Unknown, confidential, skipped, stopped and
          unanswered questions stay in the gap queue.
        </Notice>
      )}
      {session.data?.status === 'active' && (
        <Notice>
          This follow-up is still in progress.{' '}
          <Link to={`/workspace/${run.id}/interview/${sessionId}`}>Continue interview</Link>
        </Notice>
      )}
      {session.data?.status === 'finished' && !run.maintenance_started_at && (
        <Link to={`/workspace/${run.id}/interview/${sessionId}`}>Read the follow-up interview</Link>
      )}
      {proposals.data?.proposals.map(proposal => (
        <Proposal
          key={`${proposal.id}:${proposal.revision}:${proposal.status}`}
          proposal={proposal}
          refresh={() => proposals.refetch()}
        />
      ))}
      {!proposals.error && proposals.data?.proposals.length === 0 && (
        <Empty title="No public section proposal">
          <p>
            This follow-up may have captured only confidential, unknown or skipped answers, or
            created a deliberately new topic below.
          </p>
        </Empty>
      )}
      {Boolean(entries.data?.length) && (
        <section className="stack">
          <h2>New topic entries</h2>
          {entries.data?.map(entry => (
            <article className="list-row" key={entry.id}>
              <div className="grow">
                <h3>{entry.title}</h3>
                <Badge status={entry.status}>{entry.status}</Badge>
              </div>
              <Link className="btn" to={`/workspace/${run.id}/entries/${entry.id}`}>
                Review new entry
              </Link>
            </article>
          ))}
        </section>
      )}
      <Link to={`/workspace/${run.id}/follow-ups`}>
        Return to gaps and resolve answered reader requests
      </Link>
    </div>
  );
}
