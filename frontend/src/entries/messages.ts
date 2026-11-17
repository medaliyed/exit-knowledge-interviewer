export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : 'The saved entry could not be loaded. Please try again.';
export const date = (value: string) =>
  new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
