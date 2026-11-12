---
id: WIKI-ledger-db-failover
title: ledger-db failover
last_updated: 2025-02-10
authors: Tomasz Brandl, Nadia Ferrand
---
# ledger-db failover

## Trigger

Storage latency or replication lag alarm on the primary.

## Steps

1. Announce the failover in the platform channel.
2. Stop the write path at the gateway.
3. Promote replica-1 with `ledgerctl promote replica-1`.
4. Repoint the service record and re-enable writes.
5. Rebuild the old primary as a replica.

## Rollback

Promote the old primary back once storage is healthy.
