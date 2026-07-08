# OneBrain Retrieval Confidence Design

## Goal

Improve answer quality by preventing low-confidence vector matches from reaching
the LLM as evidence. The access boundary remains unchanged: permission filtering
still happens in the store and is rechecked in the retrieval service.

## Design

Add `ONEBRAIN_RETRIEVAL_MIN_SCORE`, exposed as `Settings.retrieval_min_score`.
`RetrievalService` applies this floor after permission checks and before prompt
assembly. Hits below the floor are counted as filtered evidence and excluded
from sources and model context.

If every accessible hit is below the floor, the retrieval service returns a
direct no-match response instead of calling the LLM with weak context. This
keeps cost at zero for clearly unsupported questions and avoids model answers
based on accidental embedding collisions.

Every non-greeting answer metadata event includes:

- `retrieval_min_score`
- `best_score`
- `filtered_chunks`

Greeting/direct responses also include the same fields with no best score so
clients can treat metadata consistently.

## Configuration

The default score floor is `0.05`. This filters zero, negative, and obviously
weak matches from the local hash embedder while preserving current seeded
queries. Production deployments can tune the threshold for the active embedding
provider with `ONEBRAIN_RETRIEVAL_MIN_SCORE`.

## Testing

Add retrieval tests for:

- filtering hits below the configured score floor,
- reporting `best_score`, `filtered_chunks`, and `retrieval_min_score`,
- avoiding LLM calls when all retrieved chunks are below the floor,
- preserving existing bounded-context and source behavior.
