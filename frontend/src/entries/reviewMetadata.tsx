import { Badge } from '../ui';
import type { EntrySummary, ReviewMetadata } from '../types';
export const dayAfter = (days: number) =>
  new Date(Date.now() + days * 86400000).toISOString().slice(0, 10);
export const expected = (entry: EntrySummary) => ({
  expected_version: entry.version,
  expected_maintenance_revision: entry.maintenance_revision,
});

export function Freshness({ value }: { value: ReviewMetadata }) {
  if (!value.review_by || !value.freshness) return null;
  return (
    <div className="review-metadata stack small" aria-label="Review information">
      <div className="inline">
        <Badge status={value.freshness === 'overdue' ? 'rejected' : undefined}>
          {value.freshness === 'overdue'
            ? 'Review overdue'
            : value.freshness === 'due_soon'
              ? 'Review due soon'
              : 'Review current'}
        </Badge>
        <span>Review by {value.review_by} (UTC)</span>
        {Boolean(value.open_flag_count) && (
          <span>Reader feedback pending · {value.open_flag_count}</span>
        )}
      </div>
      {value.last_reviewed_at && (
        <span>
          Last reviewed by {value.last_reviewed_by_name ?? 'Unknown'} ·{' '}
          {value.last_reviewed_at.slice(0, 10)}
        </span>
      )}
      {value.owner_name && <span>Maintained by {value.owner_name}</span>}
    </div>
  );
}
