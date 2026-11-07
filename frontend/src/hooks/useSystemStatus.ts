import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AiStatusResponse, HealthResponse } from "../api/types";

export interface SystemStatus {
  health: HealthResponse | null;
  ai: AiStatusResponse | null;
  healthError: ApiError | null;
  aiError: ApiError | null;
  loading: boolean;
}

const POLL_MS = 30_000;

/** Polls /api/health and /api/ai/status (both return status only, never data). */
export function useSystemStatus(): SystemStatus {
  const [status, setStatus] = useState<SystemStatus>({
    health: null,
    ai: null,
    healthError: null,
    aiError: null,
    loading: true,
  });

  useEffect(() => {
    let cancelled = false;
    let controller = new AbortController();
    const load = async () => {
      controller.abort();
      controller = new AbortController();
      const [h, a] = await Promise.allSettled([api.health(controller.signal), api.aiStatus(controller.signal)]);
      if (cancelled) return;
      const asErr = (r: PromiseSettledResult<unknown>) =>
        r.status === "rejected" ? (r.reason instanceof ApiError ? r.reason : new ApiError("network", "Unavailable")) : null;
      setStatus({
        health: h.status === "fulfilled" ? h.value : null,
        ai: a.status === "fulfilled" ? a.value : null,
        healthError: asErr(h),
        aiError: asErr(a),
        loading: false,
      });
    };
    void load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(id);
    };
  }, []);

  return status;
}
