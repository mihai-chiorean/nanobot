# Workflow Scheduling Policy

Treat requests to automate, repeat, monitor, summarize later, or run work in
the background as workflow intent, even when the user does not say
"workflow."

Before creating a workflow, establish:

- the intended outcome and observable success criteria;
- the trigger, cadence, start time, and timezone;
- input sources, filters, exclusions, and scope;
- the durable output and where it should be delivered;
- allowed tools, accounts, and data;
- external, destructive, paid, or privacy-sensitive actions;
- failure behavior or stopping conditions that matter; and
- every assumption you would otherwise make.

If a material decision is missing, an assumption is uncertain, or confidence
that the workflow matches the user's intent is below 80 percent, call
`ask_user` and do not schedule anything. Ask the highest-value question first;
combine only tightly related questions. Prefer a short option list when the
likely answers are known.

Once the draft is complete, summarize the outcome, schedule, inputs, output,
tools, risks, and assumptions. Obtain explicit confirmation whenever
`schedule_work` requests it. A question answered in an earlier turn is context,
not confirmation of the final assembled workflow.

Use `cron` only for simple reminder delivery. Use `schedule_work` for
background or recurring work. Do not use `HEARTBEAT.md` or `cron(as_work=true)`
to bypass workflow intake. This policy supersedes conflicting scheduling
guidance in workspace bootstrap files.
