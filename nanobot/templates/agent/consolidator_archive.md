Create a compact, private checkpoint that lets the agent resume this conversation without repeating completed reasoning or tool work.

Preserve only useful state:
- Current objective, constraints, and unresolved questions
- User facts, corrections, preferences, decisions, and commitments
- Conclusions supported by evidence or tool results
- Attempts that failed and the concrete reason they failed
- Files, resources, identifiers, and next actions needed to continue

The input may contain PRIVATE WORKING TRACE sections. Use them to recover conclusions, hypotheses, and failed approaches, but never copy the raw trace or narrate hidden reasoning. Never include passwords, tokens, cookies, authorization headers, private keys, or transient secrets. Skip implementation details that are directly recoverable from source or git history unless they are necessary to explain an unresolved failure.

Output concise bullets, one state item per line. No preamble or commentary.
If nothing noteworthy happened, output: (nothing)
