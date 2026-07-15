# What Is Orlog? (Explained for Someone With No Idea What This Is)

## The one-sentence version

Orlog is a "memory" system for AI assistants that is built to never let the AI confidently say something it can't prove — instead of guessing, it either gives you a fact-checked answer or honestly says "I don't know."

## The problem it solves

AI assistants that can "remember" things across conversations (your name, your preferences, facts you've told them before) are everywhere now. But that memory has two chronic problems:

- **It goes stale.** You tell the assistant your phone number. Six months later you tell it a new one. A lot of memory systems will happily serve up either one, with no way to know which is current.
- **It hallucinates.** The AI states something with total confidence that it actually made up, or half-remembered, or misattributed to you.

Orlog exists to fix both problems for developers who are building AI agents and need the agent's memory to be **trustworthy** — auditable, provably correct, and honest about its own limits, rather than just plausible-sounding.

## The core idea, in one analogy

Imagine a diary and a fact-checker working together:

- The **diary** is permanent and append-only. Every fact you tell it gets written down with a timestamp. You can never erase or edit an old entry — if something changes, you write a *new* entry saying so. This means the diary always preserves the full history of what was said and when.
- The **fact-checker** never trusts the diary's summary of itself. Every single time the AI wants to state something as fact, the fact-checker goes back to the raw diary entries and independently re-verifies the claim, word for word, before allowing it to be spoken aloud. If it can't verify the claim, the AI is required to say "I don't know" instead of guessing.

That's Orlog. Nothing is ever deleted or silently overwritten, and nothing is ever asserted as true without being checked against the original record — every time, not just once.

## Where the name comes from

*Orlog* (Old Norse *ørlǫg*) roughly means "the immutable layers of what has been laid down" — the fixed, unchangeable past that fate is woven from. It's a fitting name: the project's foundation is an unchangeable historical record, with everything else (search, summaries, current understanding) rebuilt on top of it on demand.

## How a single fact travels through the system

Every piece of information goes through the same six-step journey, and it can only ever end in one of two places: **verified**, or an honest **"I don't know."**

1. **Observed** — Something gets written down (e.g., "the user's email is alice@example.com"). This becomes a permanent diary entry.
2. **Interpreted** — The system organizes all the diary entries about the same thing into a timeline, so it knows which value is the *current* one and for how long each past value was true.
3. **Retrieved** — When a question comes in ("what's the user's email?"), the system searches the timeline for candidate facts that might answer it — as of whatever point in time is relevant.
4. **Asserted** — A plain-language answer ("claim") is drafted from the best candidate fact, along with a citation pointing to exactly which diary entry it came from.
5. **Verified** — An independent checker re-examines the claim against the raw diary entries (not the AI's opinion, not a cached summary) and either approves it or rejects it. If it's rejected, the system tries exactly once more with the bad evidence thrown out; if that also fails, the answer becomes an explicit **abstention** — "I don't have a verified answer to this," with a reason.
6. **Reinforced** — If the claim passed verification, that fact's "usefulness" score goes up slightly, so it's more likely to surface quickly next time. This scoring is based strictly on facts that were *actually proven useful*, never on vague guesswork about what's "probably related."

## Saying "I don't know" is a feature, not a bug

Most systems treat "I don't know" as a failure state to be avoided. Orlog treats it as a *success* — a guess that sounds confident but is actually wrong is far more dangerous than an honest admission of uncertainty. Abstaining is a first-class, intentional outcome.

## Keeping personal information safe

Before anything is written into the permanent diary, Orlog scans it for personal information — email addresses, phone numbers, ID numbers, passwords, and the like — and swaps each one out for a stand-in code name (e.g., `EMAIL_7`). The real value is locked away separately in an encrypted "vault," connected to that code name.

This means the permanent record never actually contains raw personal data — only code names — while the system can still recognize "this is the same person mentioned three weeks ago."

If someone needs to be "forgotten" (say, for a privacy request), Orlog doesn't have to touch or break the permanent historical record. It simply throws away the one encryption key tied to that person's code name. Every diary entry that used to decode to their information instantly becomes permanent, unreadable gibberish — while the rest of the record stays perfectly intact. This technique is sometimes called "crypto-shredding."

## Where AI is — and isn't — trusted

People might assume a project like this is "an AI that remembers things," but AI language models (like Claude or GPT) are deliberately used in only **two** narrow places, and neither one is ever taken at face value:

1. **Drafting the wording of an answer** from the facts that were found — but the wording is always double-checked against the raw record afterward (step 5, above) before it's allowed out the door.
2. **Guessing which stored fact a plain-English question is about** (e.g., matching "what's Anna's job?" to a fact stored as `user:42.job_title`) — but this only decides *which* fact to look up, never what the answer actually is.

The verification step that catches errors is deliberately simple, rule-based, and independent of any AI model — it can't be talked into approving something incorrect the way a language model sometimes can be.

## How someone would actually use it, day to day

Orlog is a command-line program that also runs as a small background service an AI assistant talks to. In practice, a developer would:

1. Create a new memory workspace (`orlog init`) — this generates a secret encryption key for the personal-information vault.
2. Start the background service (`orlog serve`) and connect their AI assistant to it.
3. From then on, the AI assistant can call two main abilities while chatting with a user:
   - **Remember** — save a new fact.
   - **Recall** — ask a question and get back either a verified, cited answer, or an honest "I don't know."
4. A developer can also inspect the full history behind any fact, permanently erase one person's personal data, or run a built-in self-check to confirm the installation is behaving correctly.

## Why any of this matters

The short version: most AI memory systems optimize for *sounding* helpful. Orlog optimizes for *being provably correct*, even if that sometimes means saying less. For situations where an AI's memory needs to be trusted the way a financial record or a legal document is trusted, that trade-off is the entire point.
