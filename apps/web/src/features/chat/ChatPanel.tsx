import { FormEvent, useEffect, useRef, useState } from "react";
import { Citation, problemMessage, useCancelTurnMutation, useCreateTurnMutation, Workspace } from "../../lib/api";

type Turn = {
  id: string; question: string; answer: string; citations: Citation[];
  status: "streaming" | "completed" | "failed" | "canceled"; grounding?: string;
};

export function ChatPanel({ workspace }: { workspace: Workspace }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [conversationId, setConversationId] = useState<string>();
  const [error, setError] = useState<string | null>(null);
  const source = useRef<EventSource | null>(null);
  const [createTurn] = useCreateTurnMutation();
  const [cancelTurn] = useCancelTurnMutation();
  const active = turns.find((t) => t.status === "streaming");

  useEffect(() => () => source.current?.close(), []);
  useEffect(() => { // new workspace -> new conversation
    source.current?.close();
    setTurns([]);
    setConversationId(undefined);
  }, [workspace.id]);

  const patch = (id: string, fn: (t: Turn) => Turn) =>
    setTurns((all) => all.map((t) => (t.id === id ? fn(t) : t)));

  async function ask(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || active) return;
    setError(null);
    try {
      const turn = await createTurn({ workspaceId: workspace.id, question: q, conversationId }).unwrap();
      setConversationId(turn.conversation_id);
      setQuestion("");
      setTurns((all) => [...all, { id: turn.turn_id, question: q, answer: "", citations: [], status: "streaming" }]);
      const es = new EventSource(turn.stream_url); // same-origin: session cookie is sent
      source.current = es;
      const end = (status: Turn["status"], extra: Partial<Turn> = {}) => {
        es.close();
        patch(turn.turn_id, (t) => ({ ...t, ...extra, status }));
      };
      es.addEventListener("delta", (m) =>
        patch(turn.turn_id, (t) => ({ ...t, answer: t.answer + JSON.parse(m.data).text })));
      es.addEventListener("citation", (m) =>
        patch(turn.turn_id, (t) => ({ ...t, citations: [...t.citations, JSON.parse(m.data)] })));
      // The final answer replaces streamed text: unverified citation markers are removed server-side.
      es.addEventListener("completed", (m) => {
        const d = JSON.parse(m.data);
        end("completed", { answer: d.answer, grounding: d.grounding });
      });
      es.addEventListener("canceled", () => end("canceled"));
      es.addEventListener("failed", () => end("failed"));
      es.onerror = () => { if (es.readyState !== EventSource.CLOSED) end("failed"); };
    } catch (err) {
      setError(problemMessage(err));
    }
  }

  return (
    <section className="chat">
      <h2>Ask {workspace.name}</h2>
      <ol className="turns">
        {turns.map((t) => (
          <li key={t.id}>
            <p className="q">{t.question}</p>
            <p className="a" aria-live="polite">
              {t.answer || (t.status === "streaming" ? "…" : "")}
            </p>
            {t.status === "failed" && <p className="error">The answer could not be generated.</p>}
            {t.status === "canceled" && <p className="muted">Stopped.</p>}
            {t.grounding === "insufficient_evidence" && (
              <p className="muted">No supporting evidence in documents you can access.</p>
            )}
            {t.citations.length > 0 && (
              <ul className="cites">
                {t.citations.map((c) => (
                  <li key={c.label}>
                    <strong>[{c.label}]</strong> {c.filename}, page {c.page_start}
                    {c.quote_preview && <blockquote>{c.quote_preview}</blockquote>}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ol>
      <form onSubmit={ask} className="row">
        <label className="grow">
          <span className="sr-only">Question</span>
          <input value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={2000}
                 placeholder="e.g. How many casual leave days are allowed?" disabled={!!active} />
        </label>
        {active
          ? <button type="button" onClick={() => cancelTurn(active.id)}>Stop</button>
          : <button disabled={!question.trim()}>Ask</button>}
      </form>
      {error && <p role="alert" className="error">{error}</p>}
    </section>
  );
}
