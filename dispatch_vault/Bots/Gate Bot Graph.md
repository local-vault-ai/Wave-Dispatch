---
type: bot
duty: gate
model: "wave-gate:latest"
temperature: 0
seed: 7
machine: "[[This Computer]]"
status: active
for: "[[Graph Reader]]"
gate_question: "Does () contain a link to a note written in [[double brackets]]?"
gate_examples:
  - "What does the [[Rules]] note say about saving? -> yes"
  - "Summarise [[Project Plan]]. -> yes"
  - "What is 15% of 80? -> no"
  - "Hi, how are you? -> no"
tags: [wave-dispatch/bot]
---
# Gate Bot Graph

Part of [[AI Bots]] · runs on [[This Computer]] · step in [[How Wave Dispatch Works]]

## Instructions
You are a yes/no gate. The controller asks you one question about the user's message, such as "Does this question ask for code?". Reply with exactly one word: yes or no.
