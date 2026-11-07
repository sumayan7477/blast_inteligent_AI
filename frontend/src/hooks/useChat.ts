import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ChatResponse } from "../api/types";

export type ChatMessage =
  | { id: string; role: "user"; text: string }
  | { id: string; role: "assistant"; response: ChatResponse }
  | { id: string; role: "error"; question: string; error: ApiError };

let counter = 0;
const nextId = () => `m${++counter}`;

/**
 * Conversation state lives in React memory only: nothing is written to localStorage,
 * sessionStorage, cookies or the console. Reloading the page forgets the conversation.
 */
export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [clearError, setClearError] = useState<ApiError | null>(null);
  const sessionRef = useRef<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const send = useCallback(async (question: string) => {
    const q = question.trim();
    if (!q || controllerRef.current) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setPending(true);
    setMessages((m) => [...m, { id: nextId(), role: "user", text: q }]);
    try {
      const response = await api.chat(q, sessionRef.current, controller.signal);
      sessionRef.current = response.session_id;
      setSessionId(response.session_id);
      setMessages((m) => [...m, { id: nextId(), role: "assistant", response }]);
    } catch (err) {
      if (!(err instanceof ApiError && err.kind === "aborted")) {
        const error = err instanceof ApiError ? err : new ApiError("network", "Unexpected client error.");
        setMessages((m) => [...m, { id: nextId(), role: "error", question: q, error }]);
      }
    } finally {
      controllerRef.current = null;
      setPending(false);
    }
  }, []);

  const clear = useCallback(async () => {
    controllerRef.current?.abort();
    const sid = sessionRef.current;
    sessionRef.current = null;
    setSessionId(null);
    setMessages([]);
    setClearError(null);
    if (sid) {
      try {
        await api.clearChat(sid);
      } catch (err) {
        setClearError(err instanceof ApiError ? err : new ApiError("network", "Could not clear the server session."));
      }
    }
  }, []);

  return { messages, sessionId, pending, send, clear, clearError };
}
