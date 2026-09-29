"""ADV Loop harness: the runtime layer around the untouched kernel.

The kernel (``src/adv_loop``) decides what is legal and whether proof suffices.
This package owns everything around one directive: which model session runs,
inside which container, what the harness observed the tools do, how the
attempt submission is assembled from that record, and what it cost.

Nothing here imports domain vocabulary into ``src/``; the neutrality lint does
not scan this package, so prompts and profiles may name fields freely.
"""

__version__ = "0.1.0"
HARNESS_ID = "adv-harness"
