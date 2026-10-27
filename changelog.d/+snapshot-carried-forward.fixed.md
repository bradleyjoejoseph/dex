**`maintain snapshot --project-only` reports `data.baseline.carried_forward: true`.**
Running `maintain snapshot --project-only` previously reported `false` because
the parameter was dropped when instantiating the baseline model. It is now passed
directly to the result envelope. In addition, `FilesystemStore` writes documents
atomically via a temporary file in `.dex/` and `os.replace`, and static type
checking with `mypy` and the Pydantic plugin now runs over `maintain/` and
`storage/` in CI.
