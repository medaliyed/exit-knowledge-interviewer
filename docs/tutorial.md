# Tutorial: your first run, step by step

This walks you through the whole project once, from an empty machine to a
searchable runbook. You do not need to know the code. Every step says what to
type, what you should see, and what just happened.

You will play two roles:

- the **operator**, who runs the commands;
- the **expert**, Nadia Ferrand, a made-up engineer who is leaving. The
  project ships with her made-up tickets, commits, pull requests, wiki pages
  and emails. The default expert/date filter ingests 262 non-mail artifacts.

By the end you will have interviewed "Nadia" about the things only she knows,
approved the runbook entries written from her answers, and searched them.

**Time:** 30 to 60 minutes, most of it typing answers in step 7. You can stop
the interview at any point and continue later. **Cost:** steps 6 and 7 call a
paid model through your own provider key. With the optional model settings
left blank, the other steps run locally without provider calls. If you have no key, do steps 1 to 5 and then read
[No provider key?](#no-provider-key).

> **About the output shown here.** It was captured from a real run of these
> commands on 14 November 2026. Your IDs and dates will differ. In steps 6 and
> 7 the model was replaced by the project's test stand-in, and the plan was
> cut to 4 topics, to keep the examples short. A configured model will use
> its own wording; the default plan selects 10 topics plus any additional
> topics whose sources contradict each other. Real-model quality is unverified.

Phase 5 also prints generation and message receipt lines. The migration output
and the recovery examples below were re-captured with scripted models after
those changes; the longer interview examples remain excerpts from the earlier
fixture run. Real-model quality is still unverified.

## Before you start

You need three things installed. Check each one:

```bash
uv --version        # any recent version; install from https://docs.astral.sh/uv/
docker --version    # Docker Desktop must also be running
python3 --version   # only used once, to make a token in step 9
```

Then open a terminal in the project folder. Every command in this tutorial is
run from there.

```bash
cd exit-knowledge-interviewer
```

## Step 1: create your settings file

```bash
cp .env.example .env
```

Open `.env` in an editor:

1. Find the line `GROQ_API_KEY=` and paste your key after the `=`.
   If you use another provider, add its key under its own name (for example
   `OPENAI_API_KEY=...`) and change the two model lines below to that
   provider's model names.
2. Check these two lines. They name the model for each role:

   ```bash
   EKI_MODEL_INTERVIEWER=groq/openai/gpt-oss-120b
   EKI_MODEL_DRAFTER=groq/openai/gpt-oss-120b
   ```

3. Replace the placeholder secrets, following the comments in `.env.example`.
   Keep the Postgres password in `EKI_DATABASE_URL` consistent with
   `POSTGRES_PASSWORD`, and keep the two Langfuse API-key pairs consistent.
   `LANGFUSE_ENCRYPTION_KEY` needs 64 hex characters; the example file includes
   a command to generate them.
4. For this walkthrough, leave `EKI_MODEL_ARTIFACT_EMBEDDING`,
   `EKI_MODEL_EMBEDDING` and `EKI_MODEL_ANSWERER` blank. They enable optional
   provider calls described in [Optional semantic retrieval](#optional-semantic-retrieval).

`.env` is ignored by git. Never commit it.

## Step 2: start the services

```bash
docker compose up -d
```

The first time, Docker downloads several images; this can take a few minutes.
Then check that everything is running:

```bash
docker compose ps
```

**You should see** six services, all `running` or `healthy`: `postgres`,
`clickhouse`, `redis`, `minio`, `langfuse-web` and `langfuse-worker`.

**What just happened:** you started the database (Postgres) and Langfuse, a
tool that records every model call so you can look at it later. Only Postgres
is required; the other five belong to Langfuse.

## Step 3: install the project and create the tables

```bash
uv sync
uv run eki migrate
```

**You should see:**

```text
Applied: 0001_init.sql, 0002_interview.sql, 0003_runbook.sql, 0004_access.sql, 0005_runbook_search.sql, 0006_audit.sql, 0007_mail.sql, 0008_entry_edited.sql, 0009_delivery.sql, 0010_artifact_model.sql, 0011_generation.sql
```

On an existing installation, run these commands too: `uv sync` installs the
current dependencies and `eki migrate` applies only the missing migrations,
including `0009_delivery.sql`, `0010_artifact_model.sql` and
`0011_generation.sql`. Existing runs, plans and interviews are retained.
If nothing is missing it prints `Database is up to date.`

**What just happened:** `uv sync` installed the exact library versions the
project pins. `eki migrate` created the empty tables.

## Step 4: read the expert's work

```bash
uv run eki ingest --since 2024-11-01 --until 2026-10-31
```

**You should see:**

```text
Ingested 262 artifacts and indexed 262 chunks.
ee7879f1-f5bf-412e-b57e-7ab914e8032d
```

The last line is the **run ID**. Copy yours; the next steps need it. In the
rest of this tutorial, replace `RUN_ID` with it.

**What just happened:** the program read Nadia's tickets, commits, pull
requests and wiki pages from `tests/fixtures/corpus/`, removed secrets and
private email addresses, and stored the result. With artifact embeddings left
blank as above, nothing was sent to a provider.

To include her email as well, name the folders you allow. Mail is never read
unless you do:

```bash
uv run eki ingest --since 2024-11-01 --until 2026-10-31 --mail-folder Inbox --mail-folder Sent
```

Each ingest makes a new, separate run with its own ID.

## Step 5: find what only the expert knows

```bash
uv run eki map --run RUN_ID
uv run eki gaps --run RUN_ID --limit 8
```

**You should see:**

```text
Mapped 37 topics and scored them.
```

```text
rank  score  excl   doc   rec  crit  doc status kind        n  topic
--------------------------------------------------------------------
   1   86.8  1.00  1.00  0.99  0.75  none       procedure   4  billing-batch: Clarify invoice rounding for multi-currency tenants
   2   86.0  1.00  1.00  0.98  0.75  none       system     57  report-export: report-export
   3   80.3  1.00  1.00  0.92  0.75  none       procedure   2  ledger-db: Connection pool exhausted during a load test
   4   79.5  1.00  1.00  0.79  1.00  none       incident    5  billing-batch: billing-batch run failed during the posting phase
   5   77.8  1.00  1.00  0.89  0.75  none       incident    8  billing-batch: Month-end reconciliation mismatch between billing-batch
   6   73.9  1.00  1.00  0.98  0.50  none       incident    2  ledger-db: Connection pool exhausted during a load test
   7   73.7  1.00  1.00  0.98  0.50  none       procedure   3  report-export: export/writer.py: fix(scheduler): skip export when the
   8   73.3  0.86  1.00  0.98  0.75  none       procedure   6  gateway: Add a request-id header to upstream calls
```

**How to read it.** Each row is a topic. The higher the score, the more the
company loses if Nadia leaves without explaining it.

| Column | Meaning | Row 1 says |
| --- | --- | --- |
| `score` | The overall risk, 0 to 100 | 86.8, the highest |
| `excl` | How exclusively the expert handled it (1.00 = nobody else) | only Nadia |
| `doc` | How badly it is documented (1.00 = no documentation) | nothing written down |
| `rec` | How recent the work is (1.00 = just now) | very recent |
| `crit` | How critical the work was | fairly critical |
| `doc status` | `none`, `partial` or `documented` | no wiki page |
| `kind` | procedure, incident, system, decision or contact | a procedure |
| `n` | How many artifacts belong to the topic | 4 |

**What just happened:** the 262 artifacts were grouped into 37 topics, and
each topic got a score. Still no model involved.

## Step 6: draft the interview plan

This is the first step that calls a model.

```bash
uv run eki plan --run RUN_ID
```

It can take a few minutes: the model is asked once per topic.

**You should see** something of this shape (the plan excerpt starts after the
new generation/resume line):

```text
Plan 8c46d867-6bbd-42ff-8608-69c9b6face70 (draft), 4 topics
  1. billing-batch: Clarify invoice rounding for multi-currency tenants  (gap score 86.8)
       Q: What is behind TCK-0120?
          from TCK-0120: "Nadia Ferrand: Linked the pull request."
  2. report-export: report-export  (gap score 86.0)
       Q: What is behind 6ba94ffc6cb6ddf9013843e00910086608fdf9b7?
          from 6ba94ffc6cb6ddf9013843e00910086608fdf9b7: "fix(scheduler): skip export when the ledger is mid-close"
  3. billing-batch: billing-batch run failed during the posting phase  (its sources contradict each other)
       Q: TCK-0026 says "replicas first, then the billing-batch workers", but TCK-0075 says "restart the billing-batch workers first, then the ledger-db replicas". Which is right?
          from TCK-0026: "replicas first, then the billing-batch workers"
  ...
Review it, then run: eki interview --plan 8c46d867-6bbd-42ff-8608-69c9b6face70
```

The ID on the `Plan` line is the **plan ID**. Copy yours and use it wherever
this tutorial says `PLAN_ID`.

**How to read it.**

- Each numbered line is a topic, with the reason it is in the plan.
- Each `Q:` is a question drafted for it. The `from` line under it is the
  exact text in a ticket or commit that prompted the question. A question
  whose quote cannot be found in the artifact is thrown away.
- Topic 3 is in the plan for a different reason: two tickets disagree about
  the order of a restart. The program noticed and will ask Nadia which is
  right. This question is written by code, not by the model.

**What just happened:** the highest-scoring topics were chosen, and the
drafter model wrote up to five questions for each. Completed topics were saved
as drafting progressed; the complete plan is now ready for review. If drafting
stops, use the printed generation ID and the commands in
[Recover saved work](#recover-saved-work-phase-5).

The CLI displays the plan and approves it as-is when you start `eki interview`.
To remove, add or reorder topics first, use the
[browser workspace](#optional-the-browser-workspace) or the plan-edit API.

## Step 7: the interview

Now you are Nadia.

```bash
uv run eki interview --plan PLAN_ID
```

The plan is shown once more and approved in her name, then the questions
start:

```text
Plan approved by Nadia Ferrand.
Type your answer. Other commands: /skip, /confidential ANSWER, /correct TURN NEW-TEXT, /quit (you can continue later).

[turn 1, trigger] What is behind TCK-0120?
>
```

**How to read a question.** `turn 1` is its number in the transcript.
`trigger` is the part of the runbook the question is about. Every topic has
seven parts, and the interviewer keeps asking until each one is filled or you
have said you cannot fill it:

| Part | The question behind it |
| --- | --- |
| trigger | When does this come up? |
| diagnosis | How do you find out what is wrong? |
| steps | What do you do, in order? |
| rollback | How do you undo it? |
| contacts | Who do you call? |
| pitfalls | What goes wrong if you are careless? |
| rationale | Why is it done this way? |

**What should I answer?** The corpus is made up, so there is no true answer
you could know. Invent something plausible, as in the examples below. If you
want to answer "in character", the file `tests/fixtures/expert_answers.json`
holds what the simulated Nadia would say about the planted gaps, and
`docs/fixtures-spec.md` describes those gaps.

The seven situations below cover everything you can do in an interview.

### Situation 1: you know the answer

Type it and press Enter.

```text
[turn 1, trigger] What is behind TCK-0120?
> Finance reports that a multi-currency invoice total is off by one cent.
  noted trigger: Finance reports that a multi-currency invoice total is off by one cent.

[turn 3, diagnosis] You did not mention the diagnosis. What is it?
> Compare the invoice total with the sum of its lines in the ledger export.
  noted diagnosis: Compare the invoice total with the sum of its lines in the ledger export.

[turn 5, steps] You did not mention the steps. What is it?
> Open the tenant's rounding profile. Set the mode to half-even. Re-run billing-batch for that tenant only.
  noted steps: Open the tenant's rounding profile. Set the mode to half-even. Re-run billing-batch for that tenant only.
```

`noted trigger: ...` is the interviewer telling you what it wrote down. Read
it: this text is what ends up in the runbook.

For `steps`, write separate sentences or an explicit numbered/bulleted list.
Synthesis and later corrections use the same splitter. Common abbreviations
such as `e.g.` and `Dr.` stay intact, and each resulting step retains its
citation. Other runbook sections keep the note as a whole claim.

A real model is told to fill every part an answer covers, so one good answer
can settle several parts and you may get fewer questions than shown here.

### Situation 2: you said something wrong

Correct an earlier answer by its turn number. Your answer to the question in
`turn 5` is turn 6.

```text
[turn 7, rollback] You did not mention the rollback. What is it?
> /correct 6 Open the tenant's rounding profile. Set the mode to half-even. Re-run billing-batch for that tenant only. Confirm the total with finance.
  turn 6 corrected (steps updated)

[turn 7, rollback] You did not mention the rollback. What is it?
>
```

The note is replaced with your correction, word for word, and the same
question is asked again because you have not answered it yet.

### Situation 3: you do not know

Say that you do not know. The interviewer should record the gap as an open
question; check that outcome during entry review.

```text
[turn 18, diagnosis] You did not mention the diagnosis. What is it?
> I don't remember exactly how I narrowed it down.
  left open diagnosis: The expert said: "I don't remember exactly how I narrowed it down."
```

The runbook will show this part as an open question, with your words.

### Situation 4: you do not want to answer

```text
[turn 22, rollback] You did not mention the rollback. What is it?
> /skip
  left open rollback: skipped by expert
```

The interviewer will not ask about it again.

### Situation 5: the answer is sensitive

Start the line with `/confidential`.

```text
[turn 24, contacts] You did not mention the contacts. What is it?
> /confidential The vendor engineer's private mobile is 555 0100.
  noted contacts: confidential
```

The answer is stored and flagged. It is not sent to the model, not shown in
the runbook and not returned by the API. The runbook shows "Answered
confidentially; not published."

### Situation 6: two sources disagree

```text
--- Topic 3 of 4: billing-batch run failed during the posting phase ---

[turn 30, trigger] TCK-0026 says "replicas first, then the billing-batch workers", but TCK-0075 says "restart the billing-batch workers first, then the ledger-db replicas". Which is right?
> TCK-0026 is right: restart the ledger-db replicas first, then the billing-batch workers. The note in TCK-0075 has the order backwards.
```

The program never decides which source is right. It asks you.

### Situation 7: you need a break

```text
[turn 44, trigger] TCK-0026 says ...
> /quit
Stopped. Continue with: eki interview --plan 8c46d867-6bbd-42ff-8608-69c9b6face70
```

Run the same command later, even after restarting your computer. It picks up
at the same question:

```text
$ uv run eki interview --plan PLAN_ID
Type your answer. Other commands: /skip, /confidential ANSWER, /correct TURN NEW-TEXT, /quit (you can continue later).

[turn 44, trigger] TCK-0026 says ...
>
```

### When the interview ends

After the last topic you see a summary and the command returns:

```text
Interview finished: 26 sections noted, 2 left open.
```

**What just happened:** every answer was stored as a turn, and each part of
each topic now has a note or is marked open. As the interview finished, one
draft runbook entry per topic was written from the notes. That last step uses
no model: it copies the notes and attaches the turn each one came from.

Things you may notice:

- The line `--- Topic 1 of N ---` is not printed for the first topic. The
  first question simply appears.
- A topic stops after 12 questions. Parts still missing are marked open.
- If processing pauses, you see `Processing paused. Resume the saved message.`
  The next prompt identifies the saved message and offers `/retry` or `/quit`.
  Choose `/retry` there to continue without retyping the answer. `/retry` is
  used at this recovery prompt, not as an answer to a normal interview question.
- Receipt lines such as `message ...: saved` are omitted from the longer
  conversation excerpts above. `saved` means the answer was accepted durably;
  it does not mean model processing or runbook approval is complete.

## Step 8: review the runbook entries

Nothing is published until you, as the expert, approve it. First list the
drafts:

```bash
uv run eki entries --plan PLAN_ID
```

```text
a4b2b116-1ab6-582b-951c-f58924bb85bc  draft     Clarify invoice rounding for multi-currency tenants
0fb71a2e-81f7-571e-8e85-4eee6a64445f  draft     report-export
f34c44fc-c203-573c-a712-65c8db67c7ec  draft     billing-batch run failed during the posting phase
8a5c1706-f9b5-5d24-a99b-fb8349da10c2  draft     billing-batch
```

Before review, register your CLI identity and assign the run to it. If already
configured, reuse your existing credential. Otherwise generate a random token
with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, then put it
in both `.env` values (never in source control):

```bash
EKI_API_TOKENS='[{"token":"PASTE_IT_HERE","subject":"nadia","name":"Nadia Ferrand"}]'
EKI_API_TOKEN=PASTE_IT_HERE
```

For an unassigned run, `uv run eki access --run RUN_ID --owner nadia` assigns
that registered subject. `uv run eki maintenance --run RUN_ID` shows ownership
and revisions. Reassignment requires expected owner/revision; do not repeat an
assignment that is already correct. Model configuration is unnecessary for
review. `--review-as` cannot override your authenticated identity.

Then go through entries one by one:

```bash
uv run eki review --plan PLAN_ID
```

Each entry is shown in full, followed by a question:

```text
report-export (draft, version 1) [0fb71a2e-81f7-571e-8e85-4eee6a64445f]
  trigger:
    - The nightly export finishes but the report file is empty. [interview 2cc79e68-..., turn 17]
  diagnosis:
    Open: The expert said: "I don't remember exactly how I narrowed it down."
  steps:
    - Check the export queue depth. [interview 2cc79e68-..., turn 21]
    - Restart the export worker. [interview 2cc79e68-..., turn 21]
    - Re-queue the report. [interview 2cc79e68-..., turn 21]
  rollback:
    Open: skipped by expert
  contacts:
    Open: Answered confidentially; not published.
  pitfalls:
    - Restarting during a running export duplicates rows. [interview 2cc79e68-..., turn 27]
  rationale:
    - The worker leaks connections after about a week. [interview 2cc79e68-..., turn 29]
Approve, review unchanged, withdraw, reject, edit, triage, skip, or quit?
```

**How to read it.** Each line starting with `-` is a claim. The bracket after
it is its source: the turn of the interview where you said it. `Open:` marks a
part you did not answer. Compare this entry with situations 3, 4 and 5 above:
this is where those three answers ended up.

You have five choices.

**approve** publishes the entry.

```text
Approve, review unchanged, withdraw, reject, edit, triage, skip, or quit? approve
Approved.
```

**edit** replaces one part. You are asked for the part, then for one claim per
line, then for an optional open question. Press Enter on an empty line to
finish. The entry is shown again and still needs approval.

```text
Approve, review unchanged, withdraw, reject, edit, triage, skip, or quit? edit
Section: rollback
Claim (blank to finish): Stop the export worker and restore last night's report from the archive.
Claim (blank to finish):
Open question (blank for none):
Edited; approval is required.
```

**reject** marks the entry rejected and asks why. It stays in the review
history and is excluded from search/export; an edit can return it to draft.

```text
Approve, review unchanged, withdraw, reject, edit, triage, skip, or quit? reject
Reason: Same content as the previous entry.
Rejected.
```

**skip** leaves it as a draft and moves on. **quit** stops; run the command
again to continue with the drafts that are left.

### The warning you might see

The notes are worded by the model. If a claim says something your answer did
not, a line appears under it:

```text
    - Restart the replicas after 15 minutes and notify finance. [interview ..., turn 6]
      ! not in the cited answer: 15, minutes, notify, finance
```

It lists the words and numbers of the claim that are in neither your answer
nor the question you were answering. It is a hint, not a block: check the
claim, then edit it or approve it. A harmless rewording is flagged too.
`eki entries` shows the same thing as `(2 claims to check)` next to an entry.

The example above is illustrative; in the captured run the stand-in model
copied the answers exactly, so there was nothing to flag.

### After the review

```bash
uv run eki entries --plan PLAN_ID
```

```text
a4b2b116-1ab6-582b-951c-f58924bb85bc  approved  Clarify invoice rounding for multi-currency tenants
0fb71a2e-81f7-571e-8e85-4eee6a64445f  approved  report-export  (edited by hand)
f34c44fc-c203-573c-a712-65c8db67c7ec  approved  billing-batch run failed during the posting phase
8a5c1706-f9b5-5d24-a99b-fb8349da10c2  rejected  billing-batch
```

## Step 9: use the runbook

Searching works on behalf of a registered person. The credential and ownership
set up before review in Step 8 also authorize search, Ask and export.

**9a. Check ownership.** `uv run eki maintenance --run RUN_ID` shows the owner.
If already assigned to `nadia`, keep it. An administrator/local operator can
assign an unowned run after the identity is registered; reassignments require
its current owner and run revision. See [maintenance](flows/maintenance.md).

**9b. Reuse your credential.** Keep the same `EKI_API_TOKENS` and `EKI_API_TOKEN`
values from Step 8. If you are setting up search independently, generate and
register the credential first, then assign ownership. In capture the identity's
name must match the expert; in maintenance its subject must be the current owner.

**9c. Search.**

```bash
uv run eki search "rounding invoice"
```

```text
Clarify invoice rounding for multi-currency tenants [a4b2b116-1ab6-582b-951c-f58924bb85bc] (diagnosis)
  Compare the invoice total with the sum of its lines in the ledger export.
```

Each result is an approved entry, the part that matched best, and a snippet.
The rejected entry and any drafts are never found.

**9d. Ask a question.**

```bash
uv run eki ask "In what order do I restart after a failed posting phase?"
```

```text
Restart the ledger-db replicas. [E2]
Wait until replica lag is zero. [E6]
Restart the billing-batch workers. [E1]
Re-run the posting phase. [E4]
TCK-0026 is right: restart the ledger-db replicas first, then the billing-batch workers. The note in TCK-0075 has the order backwards. [E3]
[E2] billing-batch run failed during the posting phase, steps, version 2
[E6] billing-batch run failed during the posting phase, steps, version 2
[E1] billing-batch run failed during the posting phase, steps, version 2
[E4] billing-batch run failed during the posting phase, steps, version 2
[E3] billing-batch run failed during the posting phase, trigger, version 2
```

The answer is made of your approved claims, word for word. Each `[E...]` label
is explained underneath: which entry, which part, which version. When a step
is part of the answer, the whole procedure is returned in order.

Ask about something the runbook does not cover and it says so instead of
guessing:

```bash
uv run eki ask "How do I rotate the TLS certificate?"
```

```text
The approved entries do not provide enough evidence to answer this question.
```

Expect this answer more often than you would like. Without an answering model
the program is cautious: in the captured run it also declined "How do I roll
back the rounding change?", although the entry has a rollback. When that
happens, use `eki search` with the key words instead.

**9e. Export.**

```bash
uv run eki export --run RUN_ID --output runbook.md
```

```text
Exported to runbook.md
```

`runbook.md` holds every approved entry: seven headed parts each, numbered
steps, open questions, who approved it and when, and source footnotes. Claims
that cite the same source share a footnote. It refuses to overwrite an existing file; pick a new name
or delete the old one. An excerpt:

```markdown
## report\-export

Entry: 0fb71a2e\-81f7\-571e\-8e85\-4eee6a64445f
Version: 3
Approved by: Nadia Ferrand

### Trigger

- The nightly export finishes but the report file is empty\. [^source-56b638434933-1]

### Diagnosis

**Open question:** The expert said: "I don't remember exactly how I narrowed it down\."
```

The backslashes are deliberate: text that came from people is escaped so that
it cannot change the formatting. A Markdown viewer hides them.

You are done. You have taken one expert's work history all the way to an
approved, searchable, exportable runbook.

## Optional: look at what the model was sent

Open <http://localhost:3000> and sign in with `LANGFUSE_INIT_USER_EMAIL` and
`LANGFUSE_INIT_USER_PASSWORD` from your `.env`. The project `eki` lists every
model call from steps 6 and 7, already redacted, tagged with its run, plan and
session.

## Optional: the browser workspace

You can use the browser for steps 6–9 after ingesting and mapping the run with
the CLI. Complete [step 9a](#step-9-use-the-runbook) first: assign this run to
`nadia`. Then set `EKI_PUBLIC_ORIGIN=http://localhost:8000` in `.env` and create
an account with the same subject and expert name:

```bash
uv run eki users add --email nadia.ferrand@example.com --name "Nadia Ferrand" --subject nadia
```

The command prints a single-use invitation link valid for 72 hours. The operator
hands it over directly; the application sends no email. If you already configured
a bearer identity for Nadia in step 9b, keep the same name, subject and admin flag.

Start the server in a second terminal:

```bash
uv run uvicorn eki.api.app:app --reload
```

Open the printed invitation link and choose a password of 12–128 characters.
Avoid common passwords and your name or email address. The link signs you in
when the password is set. Later, open <http://localhost:8000/> and sign in with
your email and password. A reload restores your session and current URL. Unsent
text is still lost on reload, so heed the leave-page warning.

Use the account menu to change your password or sign out. For a forgotten
password, ask the operator to run `uv run eki users reset-password --email nadia.ferrand@example.com`;
the printed reset link lasts one hour. A password change signs out other devices.
Account enable/disable and session revocation need no server restart. Restart
after changing origin, token registration or model settings. See
[the sign-in flow](flows/sign-in.md) for production configuration and troubleshooting.

| What to do | In the workspace |
| --- | --- |
| Prepare a plan | Select your handover, open **Interview plans**, then **Draft a new plan**. Inspect per-topic progress; a paused draft offers **Inspect progress** and **Resume saved generation**. |
| Review topics | Use **Move up**, **Move down**, **Remove topic** or **Add topic**, then **Approve plan & start interview**. |
| Answer | Enter **Your answer** and choose **Save answer & continue**. Use the confidential checkbox, **Skip this question**, or **Correct an earlier answer** when needed. |
| Recover | Reopen **Open conversation**. Outstanding receipts offer **Resume saved answer**; an interrupted step without one offers **Recover conversation**. |
| Review entries | After completion, choose **Review drafted entries** or the **Review entries** tab. Inspect source-turn links, open questions and flagged terms before approving. |
| Edit or reject | **Edit entry** returns the entry to draft. In the editor, separate claims/steps with a blank line. Supply a reason when choosing **Reject entry**. |
| Use approved knowledge | Open **Approved knowledge** to search or ask across your permitted entries, read citations, and download approved Markdown. |

Only submitted messages acknowledged as saved, or found through their receipt,
are durable. Unsubmitted text remains in the current tab. Owners/admins can
manage interviews and review drafts; readers see approved knowledge only.
An empty workspace usually means no run has been assigned/shared with the
account's `subject`. Navigation lists show at most 200 records.

The implementation has HTTP, client and automated browser/accessibility coverage.
A manual screen-reader walkthrough and real-provider validation remain pending. See
[the browser flow](flows/browser-workspace.md) for permissions and endpoint details.

## Optional: the web API

The browser uses the same authenticated HTTP API. Its interactive reference
is available alongside the workspace.

```bash
uv run uvicorn eki.api.app:app --reload
```

Open <http://localhost:8000/docs>, press **Authorize** and paste the token
from step 9b. Then try `POST /runbook/search` with the body
`{"query": "rounding"}`.

## No provider key?

You can still do steps 1 to 5 for real. For steps 6 to 8, watch the project
run them by itself with a stand-in model and a simulated Nadia:

```bash
uv run pytest -m db -v tests/test_phase2_acceptance.py tests/test_phase3a_acceptance.py
```

**You should see** a list of test names each ending in `PASSED`. This proves
the machinery works. It does not show you an interview, and it says nothing
about how well a real model asks questions.

## When something goes wrong

| You see | It means | Do this |
| --- | --- | --- |
| `eki: cannot reach the database; is ... running?` | Postgres is not running | Start Docker Desktop, then `docker compose up -d` |
| `eki: 1 validation error for Settings`, then `database_url Field required` | There is no `.env` here: step 1 was skipped, or you are in the wrong folder | `cd` into the project folder; redo step 1 |
| `eki: no expert given: set EKI_EXPERT_NAME and EKI_EXPERT_EMAIL` | Those lines are missing from `.env` | Copy them from `.env.example` |
| `eki: no source directory: set EKI_SOURCE_DIR to an existing folder` | That line is missing from `.env`, or you are in the wrong folder | Copy it from `.env.example`; run from the project folder |
| `eki: run ... has no topics yet; run: eki map --run ...` | You skipped `eki map` | Run it, then `eki gaps` again |
| `eki: no completed run ...` | Wrong or mistyped run ID | Copy the ID printed by `eki ingest` again |
| `eki: EKI_MODEL_DRAFTER is not set (a LiteLLM model name)` | The model lines are missing from `.env` | Redo step 1 |
| `eki: Drafting paused; completed topics are saved. Use the resume command above.` | A provider call failed while drafting | Check provider configuration, inspect the printed generation ID, then use `--resume-generation` to keep completed topics |
| `Processing paused. Resume the saved message.` | The answer is saved but model or audit processing could not finish | Choose `/retry` at the recovery prompt, or **Resume saved answer** in the browser |
| `eki: valid EKI_API_TOKEN credentials are required` | Step 9b is not done, or the two token values differ | Fix the two lines in `.env` |
| `generation inputs or configuration changed; start a new generation` | The saved draft used different source/mapping, prompt, model or drafting settings | Run `eki plan --run RUN_ID` without a resume flag to start a new generation |
| `message differs from saved submission; resume it first` | A retry changed a saved message | Resume the existing receipt, then use an explicit correction to change its answer |
| The browser shows no handovers | The account subject has no accessible run | Check `eki access --run RUN_ID --owner SUBJECT` and `eki users list` |
| Browser sign-in fails with an origin error | `EKI_PUBLIC_ORIGIN` differs from the visible browser origin | Set the exact scheme, host and port, then restart the API |
| An invitation or reset link is invalid | It expired, was used, or was replaced | Ask the operator for `eki users reset-password --email ...` |
| `only a draft can be approved or rejected; edit it first` | The entry is already approved or rejected | Edit it to return it to draft before another review decision |
| Search finds nothing | No entry is approved yet, or step 9a named a different owner than `subject` in 9b | Approve an entry; make the two names match |
| `Review refused: check the name, section and content.` | A part name was mistyped in `edit`, or the edit left a part empty | Use one of the seven part names; give at least one claim or an open question |
| `eki: cannot create output file; check permissions and that it does not exist` | The export file already exists | Choose another name |
| `source .env` fails with `command not found: Ferrand` | Your `.env` was made from an older example | Put quotes around the name: `EKI_EXPERT_NAME="Nadia Ferrand"`. The `eki` command reads `.env` by itself, so you do not need `source` at all |

## Recover saved work (Phase 5)

Drafting prints a generation ID before its model calls. This output was
captured from a synthetic three-topic check that failed on its second topic:

```text
Generation 3c57066b-cdb5-4af1-84a8-e1f0bddfc554. Resume with: eki plan --run 2e4cd7a6-6135-47f9-a706-275310dc2756 --resume-generation 3c57066b-cdb5-4af1-84a8-e1f0bddfc554
eki: Drafting paused; completed topics are saved. Use the resume command above.
```

Its progress record contained one completed topic out of three, with status
`needs_retry`. Resuming kept the first topic and completed the remaining two.

If drafting stops, inspect or resume that ID instead of creating another plan:

```bash
uv run eki plan --run RUN_ID --generation-status GENERATION_ID
uv run eki plan --run RUN_ID --resume-generation GENERATION_ID
```

Completed topics keep their original questions. If configuration or inputs have
changed, start a new generation explicitly. An unfinished generation can be
inspected but cannot yet be approved.

This output was captured when the scripted interviewer failed after accepting
an answer:

```text
  message a3447bc8-6544-4068-9d98-bac51ae34c3d: saved
  Processing paused. Resume the saved message.
```

On reconnect, the CLI finds saved messages and offers `/retry` or `/quit`.
Choose `/retry` to continue processing the saved answer without retyping it.
The HTTP API and browser workspace expose the same delivery status and resume
operations. To use the browser, start the API and open `http://localhost:8000/`;
sign in with the invited account configured in the browser section above.

## Optional semantic retrieval

These settings are independent. Leave them blank for the provider-free defaults
outside drafting/interviewing; enabling one can add provider calls and charges.

| Setting | What it controls |
| --- | --- |
| `EKI_MODEL_ARTIFACT_EMBEDDING` | Artifact embeddings at ingestion/reindexing and the interviewer's source retrieval. Models must support 256 dimensions. |
| `EKI_MODEL_EMBEDDING` | Approved runbook embeddings for search/Q&A. Models must support 256 dimensions. |
| `EKI_MODEL_ANSWERER` | Optional evidence selection for Q&A; answers still use exact approved claims and citations. |

Changing the artifact setting affects new ingestion and explicit reindexing.
Existing runs keep their recorded model, which the interviewer continues to use.
To replace one run's artifact index after selecting a model, run:

```bash
uv run eki reindex-artifacts --run RUN_ID
```

Vectors are validated in batches and the replacement commits atomically with
its model identity. A failed rebuild leaves the previous index/model pair usable.
To return an existing run to offline hashing, clear the artifact setting and
reindex it explicitly. Restart a running API after configuration changes.
Mapping/gap scoring continues to use local hashing.

Real-provider quality validation remains deferred by request. The
[evaluation guide](../evals/README.md) lists the opt-in checks; no live provider
runs are needed to follow the local defaults in this tutorial.

## Starting again

- **Another interview on the same data:** run `eki plan --run RUN_ID` again.
  It makes a new plan with its own interview.
- **Fresh data:** run `eki ingest` again. It makes a new run; the old one
  stays.
- **Wipe everything:** `docker compose down -v` deletes the database and all
  Langfuse data on this machine. There is no undo. Afterwards start again at
  step 2.

## Where to go next

- [architecture.md](architecture.md) explains how the pieces fit, with
  diagrams.
- [flows/browser-workspace.md](flows/browser-workspace.md) covers the browser workflow and access rules.
- [flows/](flows/index.md) shows what each command does inside, step by step.
- [../README.md](../README.md) is the short reference for all commands.


## After capture: maintain the handover

Use the browser's **Maintenance** tab to review due/overdue entries or reader
feedback. An admin can transfer responsibility to an active successor with a
reason; this closes capture while preserving expert history, dates and reports.
The successor edits and approves under their own identity. Entry approval sets
a review-by date (90 days by default); **Reviewed; no changes needed** renews
that date without rewriting the original approval or claims. **Withdraw**
returns a suspect approved entry to draft until reapproval.

Readers can **Report a problem** and check their safe receipt. Owner triage is
private. A report is a request for review, not automatic withdrawal or proof of
an error. Overdue knowledge stays labelled in search/export and is excluded
from Ask. See [the maintenance guide](flows/maintenance.md) for migration,
legacy-date backfill, CLI credentials and conflict recovery.
