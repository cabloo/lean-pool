# Starter admission cases

A cases directory for [`leanpool-admit`](../../docs/admission.md): files in `verify/` must be
accepted by a correct Lean server, files in `reject/` must be rejected.

```sh
leanpool-admit --server http://lean-a.example:8000 --api-key-file api-key.txt \
    --cases examples/admission-cases
```

These three pass on any Lean with Mathlib (they were checked on Lean 4.27), so they show that a
server works, that it has Mathlib, and that it does not accept everything. They do not show
that it has *your* versions. Copy the directory and add proofs that check only on the Lean and
Mathlib you pinned, and near misses that fail only there: those are the cases that catch a box
with the wrong image.
