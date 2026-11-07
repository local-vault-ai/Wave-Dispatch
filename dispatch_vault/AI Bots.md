---
type: hub
title: AI Bots
tags: [wave-dispatch/hub]
---
# AI Bots: the Wave Dispatch team

Small local models answer as one team. Every bot is a note in `Bots/`: edit its properties (`model`, `temperature`,
`machine`, `status`, a gate's `gate_question`) and the next run uses them. How a run flows: [[How Wave Dispatch Works]].

| Step | Bot | Model | Job |
|---|---|---|---|
| 1 | [[Splitter]] | [[wave-split]] | splits the question into parts (checked by Python) |
| 2 | [[Math Gate]], [[Writer Gate]], [[Notes Gate]] | [[wave-gate]] | yes/no: does this part need my bot? |
| 2 | [[Code Gate]] | [[wave-code-gate]] | yes/no: does this part need code (also "make me a game / app / website")? |
| 3 | [[Graph Reader]] | [[llama3.1 8b]] | copies the sentences that matter from the `[[notes]]` the question links to |
| 4 | [[Math Bot]] | [[qwen2.5-coder 7b]] | maths only (exact calculator; every sum is rechecked) |
| 4 | [[Code Bot]] | [[qwen2.5-coder 7b]] | code only |
| 4 | [[Writer]] | [[llama3.1 8b]] | explanations and chat, text only |
| 5 | [[Code Checker]] | [[wave-review]] | yes/no: does the code need a thorough review? |
| 5 | [[Try Again Bot]] | [[qwen2.5-coder 7b]] | says what is wrong and how to fix it; the Code Bot rewrites |
| 6 | [[Compiler]] | [[llama3.1 8b]] | only puts the answers together |

Machines: [[This Computer]] (default), [[Second Computer]] (optional) · Past runs: [[Runs]] · Receipts: [[Logs]] ·
New here? [[00 - Start Here]]

![[Bots.base]]
