---
id: WIKI-gateway-rate-limit-rollout
title: Gateway rate-limit rollout
last_updated: 2026-06-18
authors: Nadia Ferrand, Tomasz Brandl, Priya Lindqvist
---
# Gateway rate-limit rollout

## Trigger

A tier's limits change in `limits/tiers.yaml`.

## Diagnosis

Compare live limits with `gwctl limits show <tier>`.

## Steps

1. Merge the limits change.
2. Apply to the canary pool with `gwctl limits apply --pool canary`.
3. Watch the 429 rate for 15 minutes.
4. Apply to the remaining pools.

## Rollback

Revert the merge and re-apply to all pools.

## Contacts

Platform on-call. Any of the three page authors.

## Pitfalls

The canary pool caches limits for five minutes.

## Rationale

Canary first, since a wrong limit rejects paying traffic.
