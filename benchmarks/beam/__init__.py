"""BrainDB on BEAM benchmark harness.

Standards-compliant: upstream BEAM (dataset + eval script + judge prompt)
is invoked verbatim via the git submodule at ``benchmarks/beam/upstream/``.
Our code (this package) is the adapter only: dataset -> BrainDB ingest,
question -> BrainDB /agent/query, our answers -> upstream eval.

See ``benchmarks/beam/README.md`` for the full trust model.
"""
