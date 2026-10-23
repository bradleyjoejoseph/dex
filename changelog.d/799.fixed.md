**The programmatic API now reports `.dex/config.yml` diffs and moot-write warnings.**
Answers to cumulative ceiling prompts recorded at `DexEngine` construction, as
well as DuckDB connection inference warnings, are now folded onto the first
`Result` returned by the engine so library callers receive them without needing
to inspect `engine.config_diffs`, `engine.budget_warnings`, or `engine.connection_warnings`.
Subsequent results omit them so they are reported once.
