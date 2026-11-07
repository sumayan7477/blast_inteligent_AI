import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Eraser, Loader2, Lock, MessageSquareText, SendHorizontal } from "lucide-react";
import type { AiStatusResponse, ChatResponse } from "../api/types";
import { ChatMessages } from "../components/chat/ChatMessages";
import { EvidencePanel } from "../components/EvidencePanel";
import { Panel } from "../components/ui/Panel";
import { Badge } from "../components/ui/Badge";
import { useChat } from "../hooks/useChat";

export const MAX_QUESTION_CHARS = 1000;

export const SUGGESTED_QUESTIONS = [
  "How many blasts and drilled holes are recorded?",
  "Compare rig performance",
  "How many holes per site?",
  "Show the monthly drilling trend",
  "Were there any unusual drilling durations?",
  "How often do drilling problems occur?",
  "Can we predict drilling duration?",
];

function LlmStatus({ ai }: { ai: AiStatusResponse | null }) {
  if (!ai) return <Badge>LLM status unknown</Badge>;
  if (!ai.chat_uses_llm) return <Badge>LLM disabled · template answers</Badge>;
  if (ai.reachable && ai.model_available) return <Badge tone="ok">Ollama ready · {ai.model}</Badge>;
  if (ai.reachable) return <Badge tone="warn">Model {ai.model} not installed · template answers</Badge>;
  return <Badge tone="warn">Ollama offline · template answers</Badge>;
}

export function AssistantPage({ ai }: { ai: AiStatusResponse | null }) {
  const { messages, pending, send, clear, clearError, sessionId } = useChat();
  const [draft, setDraft] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const latestAssistant = useMemo(() => [...messages].reverse().find((m) => m.role === "assistant"), [messages]);
  useEffect(() => {
    if (latestAssistant) setSelectedId(latestAssistant.id);
  }, [latestAssistant]);
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length, pending]);

  const selected = messages.find((m) => m.id === selectedId && m.role === "assistant");
  const selectedResponse: ChatResponse | null = selected && selected.role === "assistant" ? selected.response : null;
  const tooLong = draft.length > MAX_QUESTION_CHARS;

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!draft.trim() || pending || tooLong) return;
    void send(draft);
    setDraft("");
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) submit();
  };

  return (
    <div className="grid min-h-0 gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <Panel
        title="AI Assistant"
        subtitle="Answers are computed from approved typed views; the local LLM only phrases them."
        icon={MessageSquareText}
        actions={
          <>
            <LlmStatus ai={ai} />
            <button type="button" className="btn" onClick={() => void clear()} disabled={!messages.length && !sessionId}>
              <Eraser className="h-3.5 w-3.5" aria-hidden /> Clear
            </button>
          </>
        }
        bodyClassName="flex min-h-[28rem] flex-col"
      >
        <div className="min-h-0 flex-1 overflow-y-auto pr-1 xl:max-h-[calc(100vh-17rem)]">
          {messages.length === 0 ? (
            <div className="space-y-3 py-2" data-testid="chat-empty">
              <p className="text-sm text-ink-300">Ask about historical blasts, drilling performance, rigs, trends or anomalies.</p>
              <div className="flex flex-wrap gap-2">
                {SUGGESTED_QUESTIONS.map((q) => (
                  <button key={q} type="button" className="btn text-left text-xs" onClick={() => void send(q)} disabled={pending}>
                    {q}
                  </button>
                ))}
              </div>
              <p className="flex items-center gap-1.5 text-xs text-ink-500">
                <Lock className="h-3 w-3" aria-hidden />
                Conversation is kept in this tab's memory only and is cleared on reload.
              </p>
            </div>
          ) : (
            <ChatMessages messages={messages} selectedId={selectedId} onSelect={setSelectedId}
                          onRetry={(q) => void send(q)} pending={pending} />
          )}
          {pending && (
            <p role="status" className="mt-4 flex items-center gap-2 text-xs text-ink-400">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> Querying the local database…
            </p>
          )}
          <div ref={endRef} />
        </div>
        {clearError && <p className="mt-2 text-xs text-signal-warn" role="alert">{clearError.message} The local view was cleared.</p>}
        <form onSubmit={submit} className="mt-3 border-t border-ink-800 pt-3">
          <label htmlFor="question" className="sr-only">Question</label>
          <textarea id="question" className="input min-h-[4.5rem] resize-y" value={draft} placeholder="Ask a question about the drilling and blasting history…"
                    onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey} disabled={pending}
                    autoComplete="off" spellCheck={false} aria-invalid={tooLong} />
          <div className="mt-2 flex items-center justify-between gap-2">
            <span className={`num text-xs ${tooLong ? "text-signal-bad" : "text-ink-500"}`}>
              {draft.length}/{MAX_QUESTION_CHARS}
            </span>
            <button type="submit" className="btn-primary" disabled={pending || !draft.trim() || tooLong}>
              <SendHorizontal className="h-3.5 w-3.5" aria-hidden /> Ask
            </button>
          </div>
        </form>
      </Panel>
      <Panel title="Evidence" subtitle="What the answer is based on — inspect before relying on it." bodyClassName="xl:max-h-[calc(100vh-12rem)] xl:overflow-y-auto">
        <EvidencePanel response={selectedResponse} />
      </Panel>
    </div>
  );
}
