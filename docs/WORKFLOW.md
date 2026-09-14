# Review, deliver and reverse an instruction change

Start with the [disposable demo](../README.md#try-it-without-personal-history).
It demonstrates inspection and decisions without personal history. Its temporary
database is not a production worker queue.

For your own data, use one explicit private config for the dashboard and worker.
Review its state directory and instruction targets before invoking a writer.

```sh
uv run selfimprove --config /absolute/private/config.toml status
uv run selfimprove --config /absolute/private/config.toml dashboard
```

The regular dashboard is at http://127.0.0.1:8765. Inspect the run, its outcome
causes and the rule's evidence. An infrastructure failure is not a verdict about
the rule. Retain unknown attribution as unknown.

## Review and approval

In Review, select the intended proposals and inspect their complete proposed
changes, destinations and combined preview. Approval binds the reviewed member
revisions and target content. A changed proposal or target requires a fresh review;
do not retry an old approval as if it still authorized the new content.

Approving records a durable command. The web process does not write instruction
files. Automatic classes can remain off throughout this manual workflow.

## Explicit delivery

Run the worker against the same configured state:

```sh
uv run selfimprove --config /absolute/private/config.toml worker --once
```

One invocation handles one queued command or interrupted instruction operation.
Inspect the command result before invoking it again. The instruction worker makes
no model calls. The separate `jobs` worker can spend model calls and is not required
to deliver an already approved instruction edit.

A direct-file result names the written target. A project result can be a commit
on the recorded delivery branch; it need not change the current checkout.
Inspect that recorded destination. Do not infer delivery from a badge alone.

After interruption, the worker reconciles durable file/ref checkpoints before
writing again. A known after-state can complete without a duplicate write.
Unknown or conflicting content blocks. Inspect the named cause and use the
recorded retry/cancel action; do not delete state to force progress.

## Rejection and queue completion

Target rejection suppresses the lesson for the selected canonical destination.
Lesson rejection suppresses it globally. Choose the intended scope and inspect
the resulting decision. Rejection does not write an instruction file.

Continue until every intended queued item is delivered, rejected, cancelled or
explicitly held with its reason. An empty queue is a state to verify, not a claim
that every rule was accepted.

## Rollback

Open the delivered proposal's rollback preview. Inspect the exact inverse and
current target content, then request the rollback. Run the explicit worker again
and inspect the operation result.

Rollback reverses the recorded contribution while preserving unrelated content.
A changed, ambiguous or conflicting block is refused. Do not restore a whole old
file snapshot to bypass the conflict. A fresh reviewed resolution may be required.

## Data and service limits

Dashboard readers and instruction workers require an existing compatible database
and do not migrate at startup. Back up state before an explicit schema upgrade.
Use one consistent SQLite backup; do not mix a main file with stale WAL sidecars.
This preview does not install a scheduler or enable automatic delivery.
