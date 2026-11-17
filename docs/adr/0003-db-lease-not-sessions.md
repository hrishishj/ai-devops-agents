# ADR-0003: Database leases instead of Service Bus sessions

**Status:** accepted (supersedes the "sessions per PR" note in the target architecture)

**Context.** Per-target ordering was to come from Service Bus sessions.

**Decision.** Queue messages carry only a job id. Mutual exclusion comes from the DB lease; ordering comes from
supersede-by-revision (an older revision's job becomes `superseded` and its result is discarded). Workers also sweep the
DB for queued/expired jobs, so a lost or duplicated message cannot lose or double-run work.

**Consequences.** Queues need no sessions (simpler, cheaper); Service Bus is an accelerator, not a source of truth. The
system still works with the queue unavailable, at polling latency.
