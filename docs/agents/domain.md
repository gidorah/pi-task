# Domain Docs

How engineering skills consume this repository’s domain documentation.

## Before exploring, read these

- `CONTEXT.md` at the repository root.
- Relevant ADRs under `docs/adr/`.

If these files do not exist, proceed silently. The domain-modeling workflow creates them when terminology or architectural decisions are resolved.

## File structure

This is a single-context repository:

```text
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

## Use the glossary’s vocabulary

Use terms as defined in `CONTEXT.md` in issues, proposals, hypotheses, and test names. Do not drift to synonyms the glossary explicitly avoids.

If a needed concept is absent, reconsider whether new language is necessary or record the gap for domain modeling.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface the conflict explicitly instead of silently overriding it.
