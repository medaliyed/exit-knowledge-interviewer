import { ApiError } from '../api';
export const message = (error: unknown) =>
  error instanceof Error ? error.message : 'The request could not be completed. Please try again.';
export const reviewMessage = (error: unknown) =>
  error instanceof ApiError && error.status === 409
    ? 'This plan changed or is no longer editable. Refresh and review it before trying again.'
    : message(error);
