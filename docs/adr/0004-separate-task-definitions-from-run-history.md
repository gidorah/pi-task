# Separate task definitions from run history

Human-readable TOML files are the source of truth for task definitions, while SQLite stores queryable run and session metadata. This keeps task configuration inspectable, editable, and versionable without forcing append-heavy operational history into configuration files.
