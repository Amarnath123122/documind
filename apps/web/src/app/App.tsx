import { useState } from "react";
import { ChatPanel } from "../features/chat/ChatPanel";
import { DocumentLibrary } from "../features/documents/DocumentLibrary";
import { WorkflowsPanel } from "../features/workflows/WorkflowsPanel";
import {
  useAuthConfigQuery, useDevLoginMutation, useLogoutMutation, useMeQuery, useUsageQuery, useWorkspacesQuery,
} from "../lib/api";

const DEV_USERS = ["alice@alpha.test", "dave@alpha.test", "carol@alpha.test", "bob@beta.test"];

export function App() {
  const me = useMeQuery();
  if (me.isLoading) return <main className="center">Loading…</main>;
  if (me.isError || !me.data) return <DevLogin />;
  return <Shell />;
}

function DevLogin() {
  const [login, { isLoading, isError }] = useDevLoginMutation();
  const { data: auth } = useAuthConfigQuery();
  if (!auth) return <main className="center">Loading…</main>;
  if (auth.mode === "oidc") {
    return (
      <main className="center">
        <h1>DocuMind</h1>
        {/* Full-page navigation: the API runs the OIDC code flow and sets the session. */}
        <a className="upload" href="/v1/auth/login">Sign in with single sign-on</a>
      </main>
    );
  }
  return (
    <main className="center">
      <h1>DocuMind</h1>
      <p className="muted">Local dev identity — disabled in production.</p>
      <div className="stack">
        {DEV_USERS.map((u) => (
          <button key={u} disabled={isLoading} onClick={() => login(u)}>Sign in as {u}</button>
        ))}
      </div>
      {isError && <p role="alert" className="error">Sign-in failed.</p>}
    </main>
  );
}

function Shell() {
  const { data: me } = useMeQuery();
  const { data: workspaces = [] } = useWorkspacesQuery();
  const [logout] = useLogoutMutation();
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<"documents" | "chat" | "workflows">("documents");
  const active = workspaces.find((w) => w.id === selected) ?? workspaces[0];
  const membership = me?.memberships.find((m) => m.tenant_id === active?.tenant_id);

  return (
    <div className="shell">
      <header>
        <strong>DocuMind</strong>
        <span className="muted">{me?.display_name}</span>
        {active && membership?.permissions.includes("usage:view") && <Usage tenantId={active.tenant_id} />}
        <button className="link" onClick={() => logout()}>Sign out</button>
      </header>
      <nav aria-label="Workspaces">
        {workspaces.map((w) => (
          <button key={w.id} aria-current={w.id === active?.id} onClick={() => setSelected(w.id)}>
            {w.name}
          </button>
        ))}
      </nav>
      <main>
        {active ? (
          <>
            <div className="tabs" role="tablist">
              {(["documents", "chat", "workflows"] as const).map((t) => (
                <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
                  {t[0].toUpperCase() + t.slice(1)}
                </button>
              ))}
            </div>
            {tab === "documents" && <DocumentLibrary workspace={active} permissions={membership?.permissions ?? []} />}
            {tab === "chat" && <ChatPanel workspace={active} />}
            {tab === "workflows" && (
              <WorkflowsPanel workspace={active} permissions={membership?.permissions ?? []} userId={me?.user_id ?? ""} />
            )}
          </>
        ) : (
          <p>No workspaces.</p>
        )}
      </main>
    </div>
  );
}

function Usage({ tenantId }: { tenantId: string }) {
  const { data } = useUsageQuery(tenantId, { pollingInterval: 30000 });
  if (!data) return null;
  const pct = (k: keyof typeof data) => Math.round((100 * Math.max(0, data[k].used)) / data[k].limit);
  return (
    <span className="muted usage" title="Organization usage this month / storage">
      AI {pct("chat_tokens")}% · Storage {pct("storage_bytes")}%
    </span>
  );
}
