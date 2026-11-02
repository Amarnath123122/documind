import { ChangeEvent, useState } from "react";
import {
  DocumentItem, problemMessage, putToBlob, TERMINAL, useCompleteUploadMutation,
  useCreateUploadMutation, useDeleteDocumentMutation, useDocumentsQuery, useSetVisibilityMutation,
  useShareDocumentMutation, Workspace,
} from "../../lib/api";

type Props = { workspace: Workspace; permissions: string[] };
const ACCEPT = ".pdf,.txt,.md,.docx";

export function DocumentLibrary({ workspace, permissions }: Props) {
  const [polling, setPolling] = useState(0);
  const { data, isLoading, error } = useDocumentsQuery(
    { workspaceId: workspace.id }, { pollingInterval: polling },
  );
  const [createUpload] = useCreateUploadMutation();
  const [completeUpload] = useCompleteUploadMutation();
  const [deleteDocument] = useDeleteDocumentMutation();
  const [setVisibility] = useSetVisibilityMutation();
  const [shareDocument] = useShareDocumentMutation();
  // UX hint only: the server checks admin-or-creator on every call.
  const canManage = permissions.includes("document:create");

  async function act(label: string, run: () => Promise<unknown>) {
    setMessage(null);
    try {
      await run();
      setMessage(label);
    } catch (err) {
      setMessage(problemMessage(err));
    }
  }

  function onShare(d: DocumentItem) {
    const email = window.prompt(`Share “${d.filename}” with (email of a member of this organization):`);
    if (email) void act(`Shared with ${email}.`, () => shareDocument({ id: d.id, email }).unwrap());
  }
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  // Hiding the control is UX only; the API is the security gate.
  const canUpload = permissions.includes("document:create");

  const inFlight = data?.items.some((d) => !TERMINAL.has(d.status)) ?? false;
  if (inFlight && polling === 0) setPolling(2000);
  if (!inFlight && polling !== 0) setPolling(0);

  async function onFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setMessage(null);
    try {
      const ticket = await createUpload({
        workspaceId: workspace.id, file, key: crypto.randomUUID(),
      }).unwrap();
      await putToBlob(ticket, file);
      await completeUpload(ticket.upload_id).unwrap();
      setMessage(`${file.name} uploaded — scanning and processing…`);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : problemMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h2>{workspace.name}</h2>
      {canUpload && (
        <div className="row">
          <label className="upload">
            <input type="file" accept={ACCEPT} onChange={onFile} disabled={busy} />
            {busy ? "Uploading…" : "Upload document"}
          </label>
          <span className="muted">PDF, TXT, MD, DOCX · up to 25 MB</span>
        </div>
      )}
      {message && <p role="status">{message}</p>}
      {isLoading && <p>Loading…</p>}
      {error && <p role="alert" className="error">{problemMessage(error)}</p>}
      <table>
        <thead><tr><th>Name</th><th>Status</th><th>Pages</th><th>Created</th>{canManage && <th>Actions</th>}</tr></thead>
        <tbody>
          {data?.items.map((d) => (
            <tr key={d.id}>
              <td>{d.filename}{d.visibility === "restricted" && <span className="badge"> restricted</span>}</td>
              <td>
                <span className={`badge ${d.status}`}>{d.status}</span>
                {d.failure_reason && <span className="error"> {d.failure_reason.replaceAll("_", " ")}</span>}
              </td>
              <td>{d.page_count ?? "—"}</td>
              <td>{new Date(d.created_at).toLocaleString()}</td>
              {canManage && (
                <td className="actions">
                  {d.status !== "deleting" && (
                    <>
                      <button className="link" onClick={() => act("Visibility updated.", () => setVisibility({
                        id: d.id, visibility: d.visibility === "restricted" ? "workspace" : "restricted",
                      }).unwrap())}>
                        {d.visibility === "restricted" ? "Open to workspace" : "Restrict"}
                      </button>
                      {d.visibility === "restricted" && (
                        <button className="link" onClick={() => onShare(d)}>Share…</button>
                      )}
                      <button className="link danger" onClick={() => {
                        if (window.confirm(`Delete “${d.filename}”? This removes its text, index and file.`)) {
                          void act("Deleting…", () => deleteDocument({ id: d.id, version: d.version }).unwrap());
                        }
                      }}>Delete</button>
                    </>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
