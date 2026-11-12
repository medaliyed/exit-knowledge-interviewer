---
id: WIKI-billing-batch-deploy
title: billing-batch deploy and schedule
last_updated: 2025-04-15
authors: Tomasz Brandl
---
# billing-batch deploy and schedule

## Schedule

The batch is started by cron at 01:30 UTC. The entry lives in `deploy/cron.yaml`.

## Steps

1. Tag the release.
2. Run `make deploy ENV=prod`.
3. Check the next cron run in the batch dashboard.

## Rollback

Re-deploy the previous tag with `make deploy TAG=<previous>`.
