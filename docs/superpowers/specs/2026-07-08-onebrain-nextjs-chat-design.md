# OneBrain Next.js Chat Migration Design

Date: 2026-07-08

## Summary

Milestone 2 migrates the core chat experience into `onebrain-web` while preserving the existing FastAPI backend as the source of truth for authentication, conversation persistence, workspace scoping, retrieval, streaming answers, sources, and token/cost metadata.

This is the first real product slice in the Next.js app. It should prove that the new frontend can handle the main assistant workflow without duplicating backend authorization or retrieval logic.

## Goals

- Build a usable Next.js chat workspace.
- Keep FastAPI as the only owner of chat state and access control.
- Support existing session-cookie authentication.
- Support existing SSE answer streaming from `POST /api/ask`.
- Show recent conversations, selected conversation messages, answer sources, and answer metadata.
- Preserve the existing FastAPI static UI during the migration.

## Non-Goals

- Do not migrate document upload or review queues in this milestone.
- Do not migrate operator, provisioning, service-key, privacy, or platform-admin screens.
- Do not change the retrieval pipeline, LLM prompt, access policy, or conversation storage semantics.
- Do not introduce a new auth provider or duplicate login/session logic in Next.js.
- Do not add assistant memory, tasks, channels, or learning-loop features yet.

## User Experience

The Next.js app becomes a real chat surface:

- Left sidebar:
  - OneBrain brand.
  - Session summary.
  - New chat action.
  - Recent conversation list.
  - Disabled navigation entries for future sections.

- Main area:
  - Empty state for a new chat.
  - Message thread for an existing conversation.
  - Streaming assistant answer while `/api/ask` emits SSE events.
  - Composer with disabled/loading states.
  - Sources and answer metadata after completion.

- Signed-out/API-unavailable states:
  - If `/api/session/me` returns 401, show a compact signed-out state with a link back to the existing FastAPI login UI.
  - If the API is unreachable, show a clear unavailable state and keep the shell usable.

The UI should feel like an operational assistant, not a marketing page. It should stay dense, calm, and scan-friendly.

## Architecture

```text
Browser
  -> Next.js app shell
  -> onebrain-web API client
  -> FastAPI endpoints
      GET    /api/session/me
      GET    /api/conversations
      GET    /api/conversations/{id}
      DELETE /api/conversations/{id}
      POST   /api/ask  (SSE stream)
  -> Python services/stores/retrieval
```

Next.js does not implement authorization decisions. It forwards the existing session cookie to FastAPI and renders what FastAPI returns.

## API Client

Extend `onebrain-web/src/lib/onebrain-api.ts` with typed helpers:

- `getSession()`
- `listConversations(scope?)`
- `getConversation(id, scope?)`
- `deleteConversation(id, scope?)`
- `askStream(payload, handlers)`

Types should mirror the existing Pydantic contract:

- `SessionInfo`
- `ConversationSummary`
- `ConversationDetail`
- `MessageOut`
- streaming events:
  - `conversation`
  - `token`
  - `sources`
  - `meta`
  - `done`

The client should keep the API base URL configurable through `ONEBRAIN_API_BASE_URL`.

## Data Flow

1. Server-render the initial shell and session state.
2. Client-side chat component loads conversations after hydration.
3. Selecting a conversation loads messages from FastAPI.
4. Sending a question calls `POST /api/ask`.
5. The client reads the SSE stream and appends tokens to the in-progress assistant message.
6. On completion, it records sources/meta and refreshes the conversation list.
7. New chat starts with no selected conversation; the first `conversation` SSE event sets the selected conversation id.

Workspace/account/space scope remains optional in this milestone. The API helper should accept scope parameters, but the UI can initially use the user's default unscoped workspace.

## Error Handling

- 401 from session: show signed-out state.
- 401/403 from chat endpoints: show a concise permission/session error.
- Network failure during stream: preserve the user's message and mark the assistant answer as failed.
- Malformed SSE event: ignore that event and keep reading where possible.
- Failed conversation load: keep the shell visible and show an inline retry state.
- Composer should prevent duplicate sends while a stream is active.

## Components

Suggested component split:

- `ChatShell`
  - owns selected conversation id, conversation list, and high-level loading states.
- `ConversationSidebar`
  - renders recent chats, new chat action, session summary.
- `MessageThread`
  - renders user and assistant messages.
- `Composer`
  - text input and send action.
- `AnswerDetails`
  - sources, chunks used, token/cost metadata.
- `SignedOutState` and `ApiUnavailableState`
  - small state components for auth/API failures.

Keep components small and local to the chat slice unless they are clearly reusable.

## Testing

Frontend:

- `npm run typecheck`
- `npm run lint`
- `npm run build`
- Add lightweight unit coverage only if the chosen setup already supports it cleanly; otherwise defer frontend tests until the first client test harness is selected.

Backend:

- Existing Python suite must remain green:
  - `uv run --python 3.12 --with-requirements requirements-dev.txt pytest -q`

Manual verification:

- FastAPI static UI still opens.
- Next.js app opens.
- Signed-out state appears when there is no valid session.
- With an active FastAPI session, recent conversations load.
- Sending a message streams tokens into the thread.
- Sources and meta appear after completion.

## Acceptance Criteria

- `onebrain-web` home screen is a real chat workspace, not just a status shell.
- Chat uses the existing FastAPI endpoints and session cookie.
- A user can start a new chat and continue an existing conversation from Next.js.
- Streaming answer tokens render progressively.
- Sources/meta render after the answer completes.
- Frontend typecheck, lint, audit, and build pass.
- Backend tests remain green.
- Existing FastAPI static UI remains available.

## Rollout

This milestone does not remove the existing static UI. After the Next.js chat surface is verified, the next migration slice should be the document library/upload flow, because chat naturally depends on seeing and managing the knowledge base.
