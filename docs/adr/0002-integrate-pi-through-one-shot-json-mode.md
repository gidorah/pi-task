# Integrate Pi through one-shot JSON mode

Each run invokes the installed Pi executable through `pi --mode json` behind a thin wrapper rather than embedding Pi's SDK or maintaining an RPC client. The one-shot process boundary limits coupling to Pi internals while still exposing structured lifecycle data and creating an ordinary persistent Pi session that can later be opened interactively.
