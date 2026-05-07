"""
DEPRECATED. Run-result persistence has been removed.

OCR results are needed only at comparison time; storing them added
latency and a failure mode without buying audit, rerun, or debug value
that the product actually uses. The collections `template_runs` and
`template_run_zones` are no longer written to.

This file is kept as an importable stub so that any stragglers in the
dependency graph fail loudly with `AttributeError` rather than
`ImportError`. It can be removed via `git rm` once nothing references
it.
"""
