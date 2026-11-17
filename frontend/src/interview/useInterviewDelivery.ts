import { useEffect, useState, useSyncExternalStore } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useWorkspace } from '../workspace';
import { refreshAfterInterview } from '../queries';
import { InterviewDelivery } from './delivery';

export function useInterviewDelivery(sessionId: string) {
  const { api, drafts, notify } = useWorkspace();
  const client = useQueryClient();
  // The route keys InterviewSession by session ID; notice/context renders must not replace an active delivery.
  const [controller] = useState(
    () =>
      new InterviewDelivery({
        api,
        drafts,
        sessionId,
        notify,
        refresh: () => refreshAfterInterview(client, sessionId),
      }),
  );
  useEffect(() => {
    controller.activate();
    return controller.deactivate;
  }, [controller]);
  return {
    ...useSyncExternalStore(controller.subscribe, controller.snapshot, controller.snapshot),
    controller,
  };
}
