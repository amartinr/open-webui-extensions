# Example: `attached_files` accumulation — before and after

A worked example with three images **A**, **B**, **C** uploaded in turns
1–3, followed by a turn 4 with no new image.

```
Legend:
  ▸ core    → <attached_files> block added by the core's add_file_context()
              (one per stored user message, relative URL)
  ▸ filter  → union block added by the image_filter inlet
              (on the LAST user message, absolute URLs)
  [A]       → <file id="A" url=".../api/v1/files/A/content"/>
```

---

## BEFORE (v2.1.0 — no pipe cleanup)

```
Turn 3 (u3 is the last message)
  u1  ▸ core [A]                    "what do you see in the first one?"
  a1  "Answer 1"
  u2  ▸ core [B]                    "and in the second one?"
      ▸ filter [A][B]               ← union: A (re-hydrated) + B
  a2  "Answer 2"
  u3  ▸ core [C]                    "and in the third one?"
      ▸ filter [A][B][C]            ← union: A + B + C  → grows every turn

Turn 4 (you type "tell me more now", no new image)
  u1  ▸ core [A]                    "what do you see in the first one?"
  a1  "Answer 1"
  u2  ▸ core [B]                    "and in the second one?"   ← CHANGED: no filter block anymore
  a2  "Answer 2"
  u3  ▸ core [C]                    "and in the third one?"   ← CHANGED: no filter block anymore
  a3  "Answer 3"
  u4  ▸ filter [A][B][C]            "tell me more now"        ← the union MOVED here

  ── The problem ──
  u2 and u3 differ between turn 3 and turn 4 (they carried the union
  block and now they do not). The cache cannot extend past a1: it
  recomputes u2→end on EVERY turn, and the block grows with each image.
```

---

## AFTER (v2.2.0 — with pipe cleanup)

```
Turn 3
  u1  ▸ core [A]                    "what do you see in the first one?"
  a1  "Answer 1"
  u2  ▸ core [B]                    "and in the second one?"   ← [A] already tagged in u1 → only B
  a2  "Answer 2"
  u3  ▸ core [C]                    "and in the third one?"   ← [A][B] already tagged → only C

Turn 4 (you type "tell me more now", no new image)
  u1  ▸ core [A]                    "what do you see in the first one?"   ✅
  a1  "Answer 1"                                                         ✅
  u2  ▸ core [B]                    "and in the second one?"             ✅ identical to turn 3
  a2  "Answer 2"                                                         ✅
  u3  ▸ core [C]                    "and in the third one?"             ✅ identical to turn 3
  a3  "Answer 3"                                                         ✅
  u4  (no block)                    "tell me more now"                   ← 0 new images → 0 block

  ── The improvement ──
  u1..u3 are byte-identical between turns → the prefix cache extends
  across the WHOLE history; only the new turn is computed.

  (If you upload a new image D in turn 4 instead:)
  u4  ▸ core [D]                    "and now this one"          ← only D; A/B/C already live
                                                                   in their own messages
```

---

## The essence

- **Before**: every turn the union block moved to the last message and
  re-tagged everything → the shared history was never equal between
  turns → short cache and a growing block.
- **After**: each file lives exactly once, in the message where it was
  uploaded → the shared history is identical turn after turn → full
  cache and a flat block.

> **Update (filter v2.12.0)**: the `image_filter` now announces pasted
> images only in the turn they are pasted (it no longer re-announces
> re-hydrated history), so the "moving union block" in the BEFORE panel
> no longer occurs even without the pipe. The pipe's cleanup still
> collapses the core's per-message blocks with the filter's current-turn
> block and deduplicates by UUID.

> **Update (v2.12.2 + v2.3.0, 2026-08-01)**: a `+` upload can still
> produce two tags in the SAME turn if the filter and the core tag
> different UUIDs of the same image (filter reused an older identical
> copy, core tagged the current upload — see filter DESIGN "Content-Hash
> Deduplication"). Fixed at the source (filter v2.12.2 reuses the
> current upload's file id from the stored message's `files`) and
> backstopped in the pipe (v2.3.0 dedups image tags by `meta["file_hash"]`
> in addition to UUID, so two UUIDs with identical bytes collapse to the
> first occurrence).

> **Update (v2.4.0, 2026-08-02)**: the pipe's dedup is now scoped **per
> user message (per turn)**, not across the conversation. The
> cross-message dedup had no remaining job after filter v2.12.0 (the
> filter only announces the current turn, so the "moving union block" is
> gone) — its only effect was hiding a **deliberate re-upload**: re-upload
> the same image in a later turn and the new tag was dropped (the agent
> never saw it) while the `+` upload still persisted a duplicate on disk.
> Now each turn keeps its own files:
>
> ```
> Turn 3: you re-upload image A (new UUID A')
>   u1  ▸ core [A]                    "first look at this"        ← original stays
>   a1  "Answer 1"
>   u2  "ok"
>   a2  "Answer 2"
>   u3  ▸ core [A']                   "look at it again"          ← re-upload visible
>       ▸ filter [A']                 (same file from both sources → one tag)
> ```
>
> The prefix u1..u2 is byte-identical to earlier turns (cache preserved);
> the re-upload turn adds its own block.
