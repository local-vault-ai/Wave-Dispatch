---
type: hub
tags: [wave-dispatch/hub]
---
# How Wave Dispatch Works

1. **Split.** The [[Splitter]] breaks your question into parts: "write is_prime, and how many primes are below
   100?" becomes (1) write is_prime, (2) how many primes are below 100. Python checks the split kept every number,
   `[[link]]` and function name; if not, the question goes on whole. One request with many details stays one part.
2. **Gates.** Every part goes through four yes/no gates ([[Math Gate]], [[Code Gate]], [[Writer Gate]], [[Notes Gate]]).
   Each part goes only to the bots whose gate said yes; a part no gate takes goes to the [[Writer]].
3. **Notes.** If a part links a note (`[[Setup]]`), the [[Graph Reader]] copies the sentences that matter, word for
   word, and the specialists get those and the note itself.
4. **Specialists.** The [[Math Bot]], [[Code Bot]] and [[Writer]] answer their parts at the same time, each only in
   its own kind of output. Maths uses an exact calculator, and Python rechecks every sum a bot writes (a wrong one
   goes back with the right number).
5. **Review loop.** The [[Code Checker]] says whether the code needs a thorough review. Yes: the [[Try Again Bot]]
   says what is wrong and how to fix it, the [[Code Bot]] rewrites, and the checker looks again (at most
   `max_rounds` times).
6. **Compile.** The [[Compiler]] puts the answers together, keeping every number and code block exactly. One answer
   alone is the final answer as it is.

Every run writes a note to [[Runs]] and a receipt to [[Logs]]: the checks (computed by Python, not by a model) and
a sha256 over everything that went in and came out. Back to [[AI Bots]].
