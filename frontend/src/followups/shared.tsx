import { ApiError } from '../api';
import type { FollowUpGap, FollowUpLimits } from '../types';
export const describe = (error: unknown) =>
  error instanceof ApiError && error.status === 409
    ? 'The saved version changed. Refresh, compare the changes, and try again.'
    : error instanceof Error
      ? error.message
      : 'The change could not be saved. Try again.';
export const gapQuestion = (gap: FollowUpGap) =>
  gap.seed_question ||
  gap.question ||
  gap.open_question ||
  `${gap.section} for ${gap.entry_title ?? 'this entry'}`;
export const defaults: FollowUpLimits = {
  topics: 3,
  questions: 6,
  model_calls: 12,
  embedding_calls: 6,
};
export const limitLabels = {
  topics: 'Topics',
  questions: 'Questions',
  model_calls: 'Model attempts',
  embedding_calls: 'Embedding attempts',
};

export function FollowUpLimitSummary({ limits }: { limits: FollowUpLimits }) {
  return (
    <p className="small muted">
      Up to {limits.topics} topics · {limits.questions} questions · {limits.model_calls} model
      attempts · {limits.embedding_calls} embedding attempts. These limits are frozen when the
      expert approves.
    </p>
  );
}
