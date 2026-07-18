/**
 * Binds a ReviewRailProvider to React state, the load-and-subscribe shape. An
 * initial load, a silent reload on every provider invalidation (the SSE nudge),
 * and an error state only on the first load or an explicit retry. It holds no
 * transport knowledge, only the seam.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { ReviewRailData } from "./contracts";
import type { ReviewRailProvider } from "./provider";

export type ReviewLoadStatus = "loading" | "ready" | "error";

export interface UseReviewDataResult {
  readonly data: ReviewRailData | null;
  readonly status: ReviewLoadStatus;
  readonly error: string | null;
  reload(): void;
}

function messageOf(error: unknown): string {
  if (error instanceof Error && error.message.length > 0) return error.message;
  return "The review layer could not load.";
}

export function useReviewData(
  provider: ReviewRailProvider,
): UseReviewDataResult {
  const [data, setData] = useState<ReviewRailData | null>(null);
  const [status, setStatus] = useState<ReviewLoadStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const activeRef = useRef<object>({});

  useEffect(() => {
    const active = {};
    activeRef.current = active;
    let cancelled = false;
    const isCurrent = () => !cancelled && activeRef.current === active;

    const load = (showLoading: boolean) => {
      if (showLoading) {
        setStatus("loading");
        setError(null);
      }
      provider
        .load()
        .then((next) => {
          if (!isCurrent()) return;
          setData(next);
          setStatus("ready");
          setError(null);
        })
        .catch((cause) => {
          if (!isCurrent()) return;
          if (showLoading) {
            setStatus("error");
            setError(messageOf(cause));
          }
        });
    };

    load(true);
    const unsubscribe = provider.subscribe(() => load(false));
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [provider, reloadToken]);

  const reload = useCallback(() => {
    setReloadToken((token) => token + 1);
  }, []);

  return { data, status, error, reload };
}
