# Wiki use: evidence and limits — 2026-09-10

The completed AI pilot `d864a5e8797a6da75ad753fa847fd412` used a one-page
synthetic wiki, not the full vault. Its persisted report cites that fixture's
limitations; the final report and safe events cannot reconstruct every search
or child session. Therefore full-wiki agent performance cannot be rated from
that run. The historical report remains unchanged and not human-verified.

A separate, read-only index check found **564 Markdown pages, zero skipped**.
Six bounded searches took approximately 2–5 ms each locally:

| Query | Returned hits (maximum 8) | Observation |
| --- | ---: | --- |
| `walker-c1` | 8 | Canonical `entities/walker-c1.md` first |
| `power-board` | 8 | Canonical `entities/power-board.md` first |
| `encoder` | 0 | English literal absent from retrieved matches |
| `电机` | 6 | Multiple robot models; applicability needs checking |
| `急停` | 5 | TienKung-related procedures; not universal robot guidance |
| `mcap` | 0 | Retrieval gap, not proof the format is irrelevant |

Index completeness is not semantic recall. Literal English/Chinese wording can
miss a relevant document; a hit may be frontmatter or a navigation reference
rather than the useful passage. These six probes are not a ground-truth recall
benchmark. No private wiki contents were sent to an external search service.

The new sandbox `robot-analysis-context` skill requires original line retrieval
after search, robot/firmware applicability checks, a narrower or bilingual
fallback, and explicit gaps. It separates observed incident evidence from wiki
procedures and hypotheses. It also addresses two demonstrated pilot mistakes:
`Z` timestamps already specify UTC, and synthetic input can have a real
model-generated report.

Next meaningful quality test: a bounded paid run using the full vault and a
human-reviewed incident, recording safe source/query references and evaluating
citation accuracy, applicability, counterevidence and the edited workflow.
Today's admission allowance was not reset or bypassed to run that test. The
skill's structure and sandbox availability are tested, not its effect on a new
model-generated diagnosis.
