**Phase checkpoint hints accurately describe unconfirmed commands.** Commands
that reach a mid-command phase checkpoint (such as `--verify` or
`--infer-by-overlap` over an already-fresh exploration cache) without earlier spend
state that the phase needs confirming instead of referencing "what remains of
the confirmed budget", and drop the note about re-profiling on re-run.
