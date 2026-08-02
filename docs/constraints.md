# Working constraints

Durable constraints on how this project is built. Recorded here because they are not
derivable from the code, and they decide what does *not* get built.

## B.6 — time and scope

Answered 2026-08-02.

- **No fixed weekly hour budget.**
- **No deadline.**
- Priorities are **quality, evidence and ROI**.
- **The absence of a deadline does not authorise scope expansion.** Nothing is implemented
  merely because there is time available.
- **Every expansion still requires measurable evidence** that it is worth doing.

### Why this is written down

With an open-ended schedule the failure mode is not running out of time, it is accumulating
unjustified technology — which is what the anti-dispersion rules in `PROJECT_SPEC.md` exist
to prevent, and what makes a portfolio project impossible to defend in an interview.

### How it has actually been applied

Measurement has killed or demoted features rather than preference:

| decision | evidence |
|---|---|
| Tier B (ISRC) not implemented | ISRC covers 17.4% of listens but the canonical dump has **no ISRC column** |
| duration scoring dropped | canonical dump has **no length column** |
| ≥30s stream-qualification rule dropped | `ms_played` present on **0.28%** of the period's listens |
| Spark not adopted for blocking | candidate space is **2.32 per listen**, p99 = 25 — a routine hash join |
| aggressive normalization confined to a fallback tier | blanket application drops single-candidate listens from **73.64% to 51.93%** |
| fallback fast-path optimisation reverted | measured **slower** (0.18s vs 0.02s per 22k strings) and incorrect on 3 cases |

The last row is the pattern in miniature: an optimisation added on a guess, measured,
found worse, and removed in the same sitting.
