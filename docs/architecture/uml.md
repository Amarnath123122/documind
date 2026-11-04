# DocuMind UML diagrams

These diagrams are written in Mermaid, which GitHub renders inline. They are derived from the code in `services/core-api/src/documind`, `apps/web/src` and `alembic/versions`. For the reasoning behind the design, see [README.md](README.md).

## 1. Use case diagram

```mermaid
flowchart LR
    Viewer([Viewer])
    Editor([Workspace editor])
    Admin([Tenant admin])
    IdP([OIDC identity provider])

    subgraph DocuMind
      UC1((Sign in))
      UC2((Browse documents))
      UC3((Ask a cited question))
      UC4((Upload document))
      UC5((Share / restrict document))
      UC6((Run report workflow))
      UC7((Approve / reject workflow))
      UC8((Delete document))
      UC9((View usage and audit))
      UC10((Export approved report))
    end

    Viewer --> UC1 & UC2 & UC3
    Editor --> UC4 & UC5 & UC6
    Admin --> UC7 & UC8 & UC9 & UC10
    Editor -. inherits .-> Viewer
    Admin -. inherits .-> Editor
    UC1 --- IdP
```

## 2. Component diagram

```mermaid
flowchart TB
    subgraph Web["apps/web (React + RTK Query)"]
      App --> DocumentLibrary & ChatPanel & WorkflowsPanel
      DocumentLibrary & ChatPanel & WorkflowsPanel --> apiTs["lib/api.ts"]
    end
    apiTs -->|"HTTPS /v1, SSE"| main

    subgraph Core["services/core-api (FastAPI modular monolith)"]
      main["main.py"] --> identity & documents & retrieval & chat & workflows & usage
      chat --> retrieval
      chat --> privacy
      workflows --> retrieval
      documents --> common
      identity --> common
      subgraph ingestion["ingestion (worker process)"]
        worker --> scanner --> extract --> indexing
      end
      indexing --> privacy & retrieval
    end

    common["common: config · db · storage · telemetry · errors"] --> PG[("Postgres + pgvector")]
    common --> Blob[("Blob storage")]
    chat --> ChatProvider{{"ChatProvider: Mock / Ollama"}}
    retrieval --> Embedder{{"Embedder: Hash / Ollama"}}
```

## 3. Class diagram: domain model

```mermaid
classDiagram
    class Role {
      <<enumeration>>
      TENANT_ADMIN
      WORKSPACE_EDITOR
      VIEWER
    }
    class Permission {
      <<enumeration>>
      WORKSPACE_READ
      DOCUMENT_READ
      DOCUMENT_CREATE
      DOCUMENT_DELETE
      DOCUMENT_SHARE
      AUDIT_READ
      USAGE_VIEW
      WORKFLOW_RUN
      WORKFLOW_APPROVE
    }
    class Membership {
      +UUID tenant_id
      +str tenant_name
      +Role role
      +permissions() frozenset~Permission~
    }
    class Principal {
      +UUID user_id
      +str email
      +str display_name
      +tuple~Membership~ memberships
      +membership(tenant_id) Membership
      +can(permission, tenant_id) bool
    }
    class Document {
      +UUID id
      +UUID tenant_id
      +UUID workspace_id
      +str filename
      +DocumentStatus status
      +int version
      +UUID created_by
      +datetime created_at
      +str mime
      +int size_bytes
      +int page_count
      +str failure_reason
      +str visibility
    }
    class DocumentStatus {
      <<enumeration>>
    }
    class Chunk {
      +int page_no
      +int ordinal
      +int char_start
      +int char_end
      +str text
    }
    class Evidence {
      +str label
      +UUID chunk_id
      +UUID document_id
      +int document_version
      +str filename
      +int page_no
      +int char_start
      +int char_end
      +str text
    }
    class Grounding {
      <<enumeration>>
    }
    class Recognizer {
      +str label
      +Pattern pattern
      +accepts(match) bool
    }
    class Redaction {
      +str text
      +dict counts
    }
    class SourceChunk {
      +UUID document_id
      +int document_version
      +str filename
      +int page_no
      +str text
    }

    Principal "1" *-- "*" Membership
    Membership --> Role
    Role ..> Permission : grants
    Document --> DocumentStatus
    Document "1" o-- "*" Chunk
    Evidence ..> Chunk : cites
    Evidence ..> Document
    Recognizer ..> Redaction : produces
    SourceChunk ..> Document
```

## 4. Class diagram: model adapters (ports and adapters)

```mermaid
classDiagram
    class ChatProvider {
      <<interface>>
      +str name
      +stream_answer(system, question, context) AsyncIterator~str~
    }
    class MockChatProvider
    class OllamaChatProvider {
      +__init__(base_url, model, num_ctx, timeout_s)
    }
    class Embedder {
      <<interface>>
      +str name
      +int dim
      +embed_documents(texts) list
      +embed_query(text) list
    }
    class HashEmbedder
    class OllamaEmbedder {
      +__init__(base_url, model)
    }
    ChatProvider <|.. MockChatProvider
    ChatProvider <|.. OllamaChatProvider
    Embedder <|.. HashEmbedder
    Embedder <|.. OllamaEmbedder
```

## 5. Entity relationship diagram (database)

```mermaid
erDiagram
    tenants ||--o{ workspaces : has
    tenants ||--o{ memberships : has
    users ||--o{ memberships : holds
    tenants ||--|| tenant_quotas : limits
    tenants ||--o{ usage_ledger : records
    tenants ||--o{ audit_events : logs
    workspaces ||--o{ documents : contains
    users ||--o{ documents : creates
    documents ||--o{ upload_sessions : "uploaded via"
    documents ||--o{ document_pages : has
    documents ||--o{ document_chunks : "split into"
    documents ||--o{ sharing_grants : "shared by"
    users ||--o{ sharing_grants : receives
    documents ||--o{ jobs : "processed by"
    workspaces ||--o{ chat_conversations : hosts
    chat_conversations ||--o{ chat_turns : contains
    chat_turns ||--o{ chat_citations : cites
    document_chunks ||--o{ chat_citations : "cited in"
    workspaces ||--o{ workflow_runs : runs
```

## 6. State machine: document lifecycle

Source: `documents/domain.py` (`ALLOWED`). Only `ready` content can be opened, cited or retrieved.

```mermaid
stateDiagram-v2
    [*] --> created
    created --> uploading
    uploading --> quarantined
    quarantined --> scanning
    scanning --> extracting
    extracting --> redacting
    redacting --> chunking
    chunking --> embedding
    embedding --> indexing
    indexing --> ready
    created --> failed
    uploading --> failed
    quarantined --> failed
    scanning --> failed
    extracting --> failed
    redacting --> failed
    chunking --> failed
    embedding --> failed
    indexing --> failed
    ready --> deleting
    failed --> deleting
    note right of deleting : any pipeline state may also go to deleting
    deleting --> deleted
    deleting --> needs_attention
    needs_attention --> deleting
    deleted --> [*]
```

## 7. State machine: workflow run

```mermaid
stateDiagram-v2
    [*] --> pending_approval : editor creates cited draft
    pending_approval --> approved : different admin approves
    pending_approval --> rejected : admin rejects
    approved --> [*] : export (sources re-checked live)
    rejected --> [*]
```

## 8. Sequence diagram: cited chat (RAG)

```mermaid
sequenceDiagram
    actor U as User
    participant W as Web (ChatPanel)
    participant API as chat.api
    participant S as chat.service
    participant R as retrieval.service
    participant DB as Postgres (RLS)
    participant M as ChatProvider
    U->>W: ask question
    W->>API: POST /v1/chat (cookie + CSRF)
    API->>S: answer(principal, question)
    S->>R: retrieve(question)
    R->>DB: FTS + vector over READY, visible chunks
    DB-->>R: ranked chunks (RRF merge)
    R-->>S: Evidence E1..En (redacted)
    alt no evidence
      S-->>W: "couldn't find" (model not called)
    else evidence found
      S->>DB: re-check sources still ready and visible
      S->>M: stream_answer(system, question, fenced evidence)
      M-->>S: token stream
      S-->>W: SSE tokens
      S->>S: keep only issued labels, set Grounding
      S->>DB: save turn + citations
      S-->>W: final event with citations
    end
```

## 9. Sequence diagram: sign-in (OIDC + PKCE)

```mermaid
sequenceDiagram
    actor U as User
    participant API as identity.api (BFF)
    participant IdP as OIDC provider
    participant DB as Postgres
    U->>API: GET /v1/auth/login
    API-->>U: redirect with state, nonce, PKCE challenge
    U->>IdP: authenticate
    IdP-->>U: redirect with code
    U->>API: GET /v1/auth/callback?code
    API->>IdP: exchange code + verifier
    IdP-->>API: ID token
    API->>API: verify alg, iss, aud, nonce
    API->>DB: upsert user by (issuer, subject)
    API-->>U: HttpOnly SameSite=Strict session cookie
```

The upload and ingestion sequence is in [README.md §3](README.md#3-upload-and-ingestion).

## 10. Deployment diagram (Azure)

```mermaid
flowchart TB
    Internet((Internet)) --> WebCA
    subgraph Azure["Azure resource group (infra/terraform)"]
      subgraph CAE["Container Apps environment"]
        WebCA["web: nginx + SPA (public)"] --> ApiCA["api: FastAPI (internal)"]
        WorkerCA["worker: ingest"]
        Migrate["migrate job: alembic"]
      end
      ApiCA & WorkerCA & Migrate --> PG[("Postgres Flexible Server + pgvector")]
      ApiCA & WorkerCA --> SA[("Storage account: quarantine / originals")]
      ApiCA & WorkerCA --> KV["Key Vault"]
      MI["Managed identity per runtime"] -.-> ApiCA & WorkerCA
    end
```
