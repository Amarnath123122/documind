import { FormEvent, useState } from "react";
import {
  problemMessage, useCreateWorkflowMutation, useDecideWorkflowMutation, useDocumentsQuery,
  useWorkflowsQuery, Workspace,
} from "../../lib/api";

type Props = { workspace: Workspace; permissions: string[]; userId: string };

export function WorkflowsPanel({ workspace, permissions, userId }: Props) {
  const { data: docs } = useDocumentsQuery({ workspaceId: workspace.id });
  const { data: runs = [] } = useWorkflowsQuery(workspace.id);
  const [createWorkflow, creating] = useCreateWorkflowMutation();
  const [decide] = useDecideWorkflowMutation();
  const [title, setTitle] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  // UX hints only: the server enforces permissions and four-eyes (author can't approve).
  const canRun = permissions.includes("workflow:run");
  const canApprove = permissions.includes("workflow:approve");
  const ready = docs?.items.filter((d) => d.status === "ready") ?? [];

  async function onDraft(e: FormEvent) {
    e.preventDefault();
    setMessage(null);
    try {
      await createWorkflow({ workspaceId: workspace.id, title: title.trim(), documentIds: picked }).unwrap();
      setTitle("");
      setPicked([]);
      setMessage("Draft created. It needs approval by another person before it can be exported.");
    } catch (err) {
      setMessage(problemMessage(err));
    }
  }

  async function onDecide(id: string, approve: boolean) {
    const note = approve ? undefined : window.prompt("Reason for rejection:") ?? undefined;
    if (!approve && !note) return;
    try {
      await decide({ id, approve, note }).unwrap();
    } catch (err) {
      setMessage(problemMessage(err));
    }
  }

  return (
    <section>
      <h2>Report workflows</h2>
      {canRun && (
        <form onSubmit={onDraft}>
          <label>Report title <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} /></label>
          <fieldset className="picker">
            <legend>Source documents (up to 10)</legend>
            {ready.map((d) => (
              <label key={d.id}>
                <input type="checkbox" checked={picked.includes(d.id)}
                       disabled={!picked.includes(d.id) && picked.length >= 10}
                       onChange={(e) => setPicked((p) => (e.target.checked ? [...p, d.id] : p.filter((x) => x !== d.id)))} />
                {" "}{d.filename}
              </label>
            ))}
          </fieldset>
          <button disabled={creating.isLoading || !picked.length || !title.trim()}>Draft report</button>
        </form>
      )}
      {message && <p role="status">{message}</p>}
      <ol className="turns">
        {runs.map((r) => (
          <li key={r.id}>
            <p className="q">{r.title} <span className={`badge ${r.status}`}>{r.status.replace("_", " ")}</span></p>
            <pre className="draft">{r.draft}</pre>
            {r.decision_note && <p className="muted">Note: {r.decision_note}</p>}
            <div className="row">
              {r.status === "pending_approval" && canApprove && r.created_by !== userId && (
                <>
                  <button onClick={() => onDecide(r.id, true)}>Approve</button>
                  <button className="link danger" onClick={() => onDecide(r.id, false)}>Reject</button>
                </>
              )}
              {r.status === "approved" && <a href={`/v1/workflows/${r.id}/export`}>Export (.md)</a>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
