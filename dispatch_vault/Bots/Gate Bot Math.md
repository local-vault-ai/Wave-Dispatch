---
type: bot
duty: gate
model: "wave-gate:latest"
temperature: 0
seed: 7
machine: "[[This Computer]]"
status: active
for: "[[Math Bot]]"
gate_question: "Does () ask for a calculation, a percentage or any number to work out?"
gate_examples:
  - "What is 15% of 80? -> yes"
  - "A file is 3.2 GB and the disk has 10 GB free. How much space is left after saving it? -> yes"
  - "How many seconds are in 3 hours? -> yes"
  - "Hi, how are you? -> no"
  - "What does RAM mean? -> no"
  - "Write a JavaScript function that sorts a list. -> no"
tags: [wave-dispatch/bot]
---
# Gate Bot Math

Part of [[AI Bots]] · runs on [[This Computer]] · step in [[How Wave Dispatch Works]]

## Instructions
You are a yes/no gate. The controller asks you one question about the user's message, such as "Does this question ask for code?". Reply with exactly one word: yes or no.
