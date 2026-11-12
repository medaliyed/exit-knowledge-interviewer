---
id: WIKI-gateway-cert-renewal
title: Gateway certificate renewal
last_updated: 2025-01-20
authors: Joon-ho Marchetti
---
# Gateway certificate renewal

## Trigger

The certificate expires within 30 days.

## Steps

1. Generate a signing request with `gwctl cert csr`.
2. Submit it through the certificate portal.
3. Install the issued certificate on the canary pool.
4. Roll it to the remaining pools.

## Rollback

Re-install the previous certificate while it is still valid.
