import { useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Freshness } from './Maintenance';
import { SaveFollowUp } from './FollowUps';
import { useWorkspace } from './workspace';
import { Badge, Button, Empty, Loading, Notice, Source } from './ui';
import type { Answer, EntrySummary, SearchResult } from './types';

const describe = (error: unknown) =>
  error instanceof Error
    ? error.message
    : 'The approved knowledge could not be reached. Please try again.';

export function Knowledge() {
  const { api, run, drafts } = useWorkspace();
  const [question, setQuestion] = useState(() => drafts.get('knowledge:question') ?? '');
  const [query, setQuery] = useState(() => drafts.get('knowledge:query') ?? '');
  const [lastQuestion, setLastQuestion] = useState('');
  const [lastQuery, setLastQuery] = useState('');
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [hits, setHits] = useState<SearchResult[] | null>(null);
  const [asking, setAsking] = useState(false);
  const [searching, setSearching] = useState(false);
  const [askError, setAskError] = useState('');
  const [searchError, setSearchError] = useState('');
  const [downloadError, setDownloadError] = useState('');
  const [downloading, setDownloading] = useState<string | null>(null);
  const entries = useQuery({
    queryKey: ['approved-entries', run.id],
    queryFn: () => api.request<EntrySummary[]>(`/runbook/entries?run_id=${run.id}`),
  });

  async function ask(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || asking) return;
    setAsking(true);
    setAskError('');
    setAnswer(null);
    setLastQuestion(question.trim());
    try {
      setAnswer(await api.request<Answer>('/runbook/ask', 'POST', { question: question.trim() }));
    } catch (caught) {
      setAskError(describe(caught));
    } finally {
      setAsking(false);
    }
  }
  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || searching) return;
    setSearching(true);
    setSearchError('');
    setHits(null);
    setLastQuery(query.trim());
    try {
      setHits(
        (
          await api.request<{ results: SearchResult[] }>('/runbook/search', 'POST', {
            query: query.trim(),
            limit: 20,
          })
        ).results,
      );
    } catch (caught) {
      setSearchError(describe(caught));
    } finally {
      setSearching(false);
    }
  }
  async function download(id?: string) {
    const key = id ?? 'handover';
    setDownloading(key);
    setDownloadError('');
    try {
      await api.download(
        id ? `/entries/${id}/markdown` : `/runbook/export?run_id=${run.id}`,
        id ? `eki-entry-${id}.md` : `eki-handover-${run.id}.md`,
      );
    } catch (caught) {
      setDownloadError(describe(caught));
    } finally {
      setDownloading(null);
    }
  }
  const entryLink = (id: string, section?: string) =>
    `/workspace/${run.id}/entries/${id}${section ? `#section-${section}` : ''}`;
  const stepsAnswer =
    answer?.evidence.length && answer.evidence.every(item => item.section === 'steps');

  return (
    <div className="stack page-content knowledge-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">A lasting reference</p>
          <h1>Approved knowledge</h1>
          <p className="page-intro">
            Every statement here has been explicitly approved by its reviewer. Search and ask cover
            all handovers shared with you; the entry list and handover download are for the selected
            handover.
          </p>
        </div>
        <Button
          disabled={Boolean(downloading) || !entries.data?.length}
          onClick={() => void download()}
        >
          {downloading === 'handover' ? 'Preparing download…' : 'Download handover as Markdown'}
        </Button>
      </div>
      {downloadError && <Notice error>{downloadError}</Notice>}
      <section className="knowledge-section stack" aria-labelledby="ask-title">
        <div>
          <h2 id="ask-title">Ask</h2>
          <p className="small muted">
            Answers use exact approved statements, with a source for each one.
          </p>
        </div>
        <form className="knowledge-form" onSubmit={event => void ask(event)}>
          <div className="form-field grow">
            <label htmlFor="knowledge-question">Your question</label>
            <input
              id="knowledge-question"
              required
              type="text"
              value={question}
              disabled={asking}
              onChange={event => {
                setQuestion(event.target.value);
                drafts.set('knowledge:question', event.target.value);
              }}
              placeholder="In what order do I restart after a failed posting phase?"
              aria-describedby="ask-scope"
            />
            <p className="field-help" id="ask-scope">
              Across approved entries in all your permitted handovers.
            </p>
          </div>
          <Button variant="primary" type="submit" disabled={asking || !question.trim()}>
            {asking ? 'Looking for approved evidence…' : 'Ask'}
          </Button>
        </form>
        {askError && <Notice error>{askError}</Notice>}
        {asking && (
          <p role="status" className="muted">
            Checking the approved runbook. This may take a moment.
          </p>
        )}
        {answer && (
          <div className="card answer-card stack" aria-live="polite">
            <p className="eyebrow">
              {answer.status === 'answered'
                ? 'Answer · approved statements, in order'
                : 'No supported answer yet'}
            </p>
            <p className="small muted">For: {lastQuestion}</p>
            {answer.status === 'answered' ? (
              <>
                {stepsAnswer ? (
                  <ol className="steps answer-statements">
                    {answer.evidence.map(item => (
                      <li key={item.id}>
                        <span className="claim">
                          {item.text}{' '}
                          <a
                            className="evidence-tag"
                            href={`#evidence-${item.id}`}
                            aria-label={`Evidence ${item.id}`}
                          >
                            {item.id}
                          </a>
                        </span>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <ul className="answer-statements stack">
                    {answer.evidence.map(item => (
                      <li key={item.id}>
                        <p className="claim">
                          {item.text}{' '}
                          <a
                            className="evidence-tag"
                            href={`#evidence-${item.id}`}
                            aria-label={`Evidence ${item.id}`}
                          >
                            {item.id}
                          </a>
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
                {!answer.evidence.length && <p className="preserve-lines">{answer.answer}</p>}
                <section className="evidence-section stack" aria-labelledby="evidence-title">
                  <h3 className="eyebrow" id="evidence-title">
                    Evidence
                  </h3>
                  <ol className="evidence-list">
                    {answer.evidence.map(item => (
                      <li key={item.id} id={`evidence-${item.id}`} className="evidence-item">
                        <div className="inline">
                          <span className="evidence-tag">{item.id}</span>
                          <Link to={entryLink(item.entry_id, item.section)}>{item.title}</Link>
                        </div>
                        <p className="small muted">
                          {item.section} · version {item.version} · statement {item.claim_index + 1}
                        </p>
                        {item.maintenance && <Freshness value={item.maintenance} />}
                        <p>{item.text}</p>
                        <div className="source-list">
                          {item.citations.map((citation, index) => (
                            <Source citation={citation} key={index} />
                          ))}
                        </div>
                      </li>
                    ))}
                  </ol>
                </section>
              </>
            ) : (
              <>
                <p className="gap-state">
                  <strong>
                    {answer.reason === 'review_required'
                      ? 'Matching knowledge needs review before it can support an answer.'
                      : answer.reason === 'evidence_changed'
                        ? 'The evidence or your access changed while answering. Ask again using current approved knowledge.'
                        : answer.reason === 'context_limit'
                          ? 'The relevant procedure exceeds the answer context. Open its entry or narrow your question.'
                          : 'The approved runbook does not cover this yet.'}
                  </strong>
                </p>
                <p className="muted">
                  Try searching for key words below. The answer will not be filled in by guessing.
                </p>
                {answer.reason === 'review_required' ? (
                  <p>
                    {run.can_manage ? (
                      <Link to={`/workspace/${run.id}/maintenance`}>Open maintenance review</Link>
                    ) : (
                      'Ask the handover owner to review the matching knowledge.'
                    )}
                  </p>
                ) : (
                  !['evidence_changed', 'context_limit'].includes(answer.reason ?? '') && (
                    <SaveFollowUp key={lastQuestion} question={lastQuestion} />
                  )
                )}
              </>
            )}
            {answer.open_questions.length > 0 && (
              <section className="open-questions stack">
                <h3>Related gaps that are still open</h3>
                <ul>
                  {answer.open_questions.map((gap, index) => (
                    <li key={`${gap.entry_id}-${gap.section}-${index}`}>
                      <p>{gap.text}</p>
                      <Link className="small" to={entryLink(gap.entry_id, gap.section)}>
                        {gap.section} · version {gap.version} · read entry
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        )}
      </section>
      <section className="knowledge-section stack" aria-labelledby="search-title">
        <h2 id="search-title">Search</h2>
        <form className="knowledge-form" onSubmit={event => void search(event)}>
          <div className="form-field grow">
            <label htmlFor="knowledge-search">Key words</label>
            <input
              id="knowledge-search"
              required
              type="search"
              value={query}
              disabled={searching}
              onChange={event => {
                setQuery(event.target.value);
                drafts.set('knowledge:query', event.target.value);
              }}
              placeholder="A system name, a ticket number, a procedure…"
            />
          </div>
          <Button type="submit" disabled={searching || !query.trim()}>
            {searching ? 'Searching…' : 'Search'}
          </Button>
        </form>
        {searchError && <Notice error>{searchError}</Notice>}
        {searching && (
          <p role="status" className="muted">
            Searching approved entries…
          </p>
        )}
        {hits !== null && (
          <div aria-live="polite">
            {hits.length ? (
              <>
                <p className="small muted">
                  {hits.length} {hits.length === 1 ? 'result' : 'results'} for “{lastQuery}” across
                  your permitted handovers.
                </p>
                <ul className="search-results">
                  {hits.map((hit, index) => (
                    <li
                      className="list-row search-result"
                      key={`${hit.id}-${hit.section}-${index}`}
                    >
                      <div>
                        <h3>
                          <Link to={entryLink(hit.id, hit.section)}>{hit.title}</Link>
                        </h3>
                        <p className="small muted">
                          Matched in {hit.section} · v{hit.version}
                        </p>
                        <Freshness value={hit} />
                      </div>
                      <p className="claim preserve-lines">{hit.snippet}</p>
                      <div className="source-list">
                        {hit.citations.map((citation, at) => (
                          <Source citation={citation} key={at} />
                        ))}
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <Empty title="No matching approved entry">
                <p>
                  No approved entry matched “{lastQuery}”. Try a system name or a ticket number, or
                  ask a full question above.
                </p>
              </Empty>
            )}
          </div>
        )}
      </section>
      <section className="knowledge-section stack" aria-labelledby="approved-list-title">
        <div>
          <h2 id="approved-list-title">Approved in this handover</h2>
          <p className="small muted">
            {run.expert_name} · only entries currently approved for readers.
          </p>
        </div>
        {entries.isPending ? (
          <Loading />
        ) : entries.error ? (
          <Notice error>
            {describe(entries.error)}{' '}
            <Button onClick={() => void entries.refetch()}>Try again</Button>
          </Notice>
        ) : entries.data?.length ? (
          <ul className="approved-entry-list">
            {entries.data.map(entry => (
              <li className="list-row" key={entry.id}>
                <div className="grow">
                  <h3>
                    <Link to={entryLink(entry.id)}>{entry.title}</Link>
                  </h3>
                  <div className="inline small muted">
                    <span>v{entry.version}</span>
                    <Freshness value={entry} />
                    <Badge status="approved">Approved</Badge>
                  </div>
                </div>
                <Button
                  disabled={Boolean(downloading)}
                  aria-label={`Download ${entry.title} as Markdown`}
                  onClick={() => void download(entry.id)}
                >
                  {downloading === entry.id ? 'Preparing…' : 'Markdown'}
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <Empty title="Nothing has been approved yet">
            <p>
              Entries appear here once{' '}
              {run.maintenance_started_at ? (run.owner_name ?? 'the maintainer') : run.expert_name}{' '}
              approves them. You can still search other handovers shared with you.
            </p>
          </Empty>
        )}
      </section>
    </div>
  );
}
