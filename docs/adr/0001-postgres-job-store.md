# ADR-0001: PostgreSQL job store instead of Cosmos DB + Redis

**Status:** accepted

**Context.** Idempotency, leases, supersede-on-new-push and an append-only audit trail all need transactions and unique
constraints. The previous design split this across Redis (idempotency, fail-open) and Cosmos (memory).

**Decision.** One relational store (PostgreSQL in Azure, SQLite in tests) behind `JobStore`. Idempotency is a unique key;
leasing is a conditional `UPDATE`; supersede happens in the same transaction as enqueue.

**Consequences.** One fewer failure mode and no fail-open path. Horizontal scale is bounded by Postgres (ample for this
workload). Schema changes need migrations (Alembic, pending). Vector memory, if needed later, can use `pgvector`.
