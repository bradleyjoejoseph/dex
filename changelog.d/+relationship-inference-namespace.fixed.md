**`explore relationships` prefers candidate parents in the child's namespace.**
When multiple candidate parents share an entity name, inference now prefers the
parent in the child's own namespace (same catalog and schema, then same catalog)
and lowers cross-catalog confidence. Verified joins with at least 50% orphans
scale confidence below 0.5 and emit an explanatory note stating that the shared
column name is not evidence of a shared key.
