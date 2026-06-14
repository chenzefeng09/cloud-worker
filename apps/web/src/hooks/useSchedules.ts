import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { CronJob } from '../api/types';

export interface UseSchedulesResult {
  schedules: CronJob[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useSchedules(): UseSchedulesResult {
  const [schedules, setSchedules] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    api.schedules
      .list()
      .then((res) => {
        // BFF passes through cron-runner's bare list[CronJobOut]
        setSchedules(Array.isArray(res) ? res : []);
      })
      .catch((e) => {
        setError((e as Error).message ?? '加载定时任务失败');
      })
      .finally(() => {
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { schedules, loading, error, refresh };
}
