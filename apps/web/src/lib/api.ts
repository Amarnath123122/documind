import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

export type Membership = { tenant_id: string; tenant_name: string; role: string; permissions: string[] };
export type Me = { user_id: string; email: string; display_name: string; memberships: Membership[] };
export type Workspace = { id: string; tenant_id: string; name: string };
export type DocumentItem = {
  id: string; workspace_id: string; filename: string; status: string; version: number; created_at: string;
  mime: string | null; size_bytes: number | null; page_count: number | null; failure_reason: string | null;
  visibility: "workspace" | "restricted";
};
export type WorkflowRun = {
  id: string; workspace_id: string; title: string; draft: string; created_by: string;
  status: "pending_approval" | "approved" | "rejected"; decision_note: string | null;
  sources: { document_id: string; filename: string }[]; created_at: string;
};
export type UsageSummary = Record<"chat_tokens" | "storage_bytes", { used: number; limit: number }>;
export type UploadTicket = {
  upload_id: string; document_id: string; upload_url: string;
  required_headers: Record<string, string>; expires_at: string; max_bytes: number;
};
export type TurnCreated = { turn_id: string; conversation_id: string; stream_url: string };
export type Citation = {
  label: string; document_id: string; filename: string; page_start: number; page_end: number;
  quote_preview: string | null;
};
export const TERMINAL =new Set(["ready", "failed", "deleted", "needs_attention"]);
export type DocumentPage = { items: DocumentItem[]; next_cursor: string | null };
export type Problem = { title: string; status: number; detail: string; code: string; trace_id: string };

function csrf(): string {
  return document.cookie.split("; ").find((c) => c.startsWith("dm_csrf="))?.slice(8) ?? "";
}

// Server state lives here (RTK Query); UI state stays in components.
export const api = createApi({
  reducerPath: "api",
  baseQuery: fetchBaseQuery({
    baseUrl: "/v1",
    credentials: "same-origin",
    prepareHeaders: (h) => {
      h.set("X-CSRF-Token", csrf());
      return h;
    },
  }),
  tagTypes: ["Me", "Documents", "Usage", "Workflows"],
  endpoints: (b) => ({
    me: b.query<Me, void>({ query: () => "/me", providesTags: ["Me"] }),
    authConfig: b.query<{ mode: "dev" | "oidc" }, void>({ query: () => "/auth/config" }),
    devLogin: b.mutation<void, string>({
      query: (email) => ({ url: "/dev/login", method: "POST", body: { email } }),
      invalidatesTags: ["Me", "Documents"],
    }),
    logout: b.mutation<void, void>({
      query: () => ({ url: "/auth/logout", method: "POST" }),
      invalidatesTags: ["Me", "Documents"],
    }),
    workspaces: b.query<Workspace[], void>({ query: () => "/workspaces", providesTags: ["Me"] }),
    documents: b.query<DocumentPage, { workspaceId: string; cursor?: string }>({
      query: ({ workspaceId, cursor }) => ({
        url: `/workspaces/${workspaceId}/documents`,
        params: cursor ? { cursor, limit: 25 } : { limit: 25 },
      }),
      providesTags: ["Documents"],
    }),
    createUpload: b.mutation<UploadTicket, { workspaceId: string; file: File; key: string }>({
      query: ({ workspaceId, file, key }) => ({
        url: `/workspaces/${workspaceId}/uploads`, method: "POST",
        headers: { "Idempotency-Key": key },
        body: { filename: file.name, size_bytes: file.size, mime: mimeOf(file) },
      }),
      invalidatesTags: ["Documents"],
    }),
    createTurn: b.mutation<TurnCreated, { workspaceId: string; question: string; conversationId?: string }>({
      query: ({ workspaceId, question, conversationId }) => ({
        url: "/chat/turns", method: "POST",
        body: { workspace_id: workspaceId, question, conversation_id: conversationId ?? null },
      }),
    }),
    cancelTurn: b.mutation<void, string>({
      query: (turnId) => ({ url: `/chat/turns/${turnId}/cancel`, method: "POST" }),
    }),
    deleteDocument: b.mutation<DocumentItem, { id: string; version: number }>({
      query: ({ id, version }) => ({
        url: `/documents/${id}`, method: "DELETE", headers: { "If-Match": `"${version}"` },
      }),
      invalidatesTags: ["Documents", "Usage"],
    }),
    setVisibility: b.mutation<DocumentItem, { id: string; visibility: DocumentItem["visibility"] }>({
      query: ({ id, visibility }) => ({ url: `/documents/${id}`, method: "PATCH", body: { visibility } }),
      invalidatesTags: ["Documents"],
    }),
    shareDocument: b.mutation<void, { id: string; email: string }>({
      query: ({ id, email }) => ({ url: `/documents/${id}/grants`, method: "POST", body: { email } }),
    }),
    usage: b.query<UsageSummary, string>({
      query: (tenantId) => `/tenants/${tenantId}/usage`, providesTags: ["Usage"],
    }),
    workflows: b.query<WorkflowRun[], string>({
      query: (workspaceId) => ({ url: "/workflows", params: { workspace_id: workspaceId } }),
      providesTags: ["Workflows"],
    }),
    createWorkflow: b.mutation<WorkflowRun, { workspaceId: string; title: string; documentIds: string[] }>({
      query: ({ workspaceId, title, documentIds }) => ({
        url: "/workflows", method: "POST",
        body: { workspace_id: workspaceId, title, document_ids: documentIds },
      }),
      invalidatesTags: ["Workflows"],
    }),
    decideWorkflow: b.mutation<WorkflowRun, { id: string; approve: boolean; note?: string }>({
      query: ({ id, approve, note }) => ({
        url: `/workflows/${id}/${approve ? "approve" : "reject"}`, method: "POST", body: { note: note ?? null },
      }),
      invalidatesTags: ["Workflows"],
    }),
    completeUpload: b.mutation<DocumentItem, string>({
      query: (uploadId) => ({ url: `/uploads/${uploadId}/complete`, method: "POST" }),
      invalidatesTags: ["Documents"],
    }),
  }),
});

export const {
  useMeQuery, useAuthConfigQuery, useDevLoginMutation, useLogoutMutation, useWorkspacesQuery,
  useDocumentsQuery, useCreateUploadMutation, useCompleteUploadMutation,
  useCreateTurnMutation, useCancelTurnMutation, useDeleteDocumentMutation,
  useSetVisibilityMutation, useShareDocumentMutation, useUsageQuery,
  useWorkflowsQuery, useCreateWorkflowMutation, useDecideWorkflowMutation,
} = api;

// Browsers often report "" for .md; the server re-checks the real bytes anyway.
export function mimeOf(file: File): string {
  if (file.type) return file.type;
  return file.name.toLowerCase().endsWith(".md") ? "text/markdown" : "application/octet-stream";
}

/** PUT bytes straight to blob storage with the short-lived, write-only capability. */
export async function putToBlob(ticket: UploadTicket, file: File, signal?: AbortSignal) {
  const res = await fetch(ticket.upload_url, {
    method: "PUT", body: file, headers: ticket.required_headers, signal,
    credentials: "omit", // the SAS is the only credential; never send the session cookie
  });
  if (!res.ok) throw new Error(`Storage rejected the upload (${res.status}).`);
}

export function problemMessage(err: unknown): string {
  const data = (err as { data?: Problem })?.data;
  return data?.detail ?? "Request failed.";
}
