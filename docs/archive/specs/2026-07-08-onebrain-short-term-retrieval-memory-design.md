# OneBrain Short-Term Retrieval Memory Design

## Goal

Improve follow-up questions by giving retrieval more recent user context without
adding persistent memory tables, migrations, or UI.

## Design

`RetrievalService.answer_stream` builds the vector-search query from the current
question plus the last three user turns from the conversation history. Assistant
turns are excluded from the retrieval query because they may contain generated
text and should not steer evidence selection. Each user turn is normalized and
bounded to 500 characters to avoid bloating embedding calls.

The retrieved chunks are still permission-filtered by the store and rechecked in
the retrieval service. History only helps find relevant evidence; it never widens
access.

Answer metadata includes `history_user_turns_used` so the UI and future traces
can distinguish first-turn retrieval from follow-up retrieval.

## Testing

Add tests for:

- using the last three user turns only,
- excluding assistant turns from retrieval query construction,
- truncating long history turns,
- reporting `history_user_turns_used` in answer metadata.
