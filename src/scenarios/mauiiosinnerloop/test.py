'''
MAUI iOS Inner Loop (Debug End-2-End) Time Measurement
Orchestrates first build-deploy-startup → file edit → incremental build-deploy-startup → parse binlogs and startup times.
'''
import os
import sys
from shared.runner import TestTraits, Runner

EXENAME = 'MauiiOSInnerLoop'

# Sentinel filename written by setup_helix.py when the work item must be
# skipped due to missing infrastructure (e.g. iOS device code-signing
# materials not present on this Helix queue). Keep in sync with
# setup_helix.SKIP_SENTINEL.
_SKIP_SENTINEL = "SKIPPED.flag"


def _check_skip_sentinel():
    """Exit 0 with a clear log message if a skip sentinel is present.

    Looked for in HELIX_WORKITEM_ROOT (where setup_helix.py writes it) and
    in the current working directory as a fallback. Emitting an exit-0
    causes the Helix work item to report PASS so the build stays green for
    documented infra gaps; the reason text is preserved in the log.
    """
    candidates = []
    workitem_root = os.environ.get("HELIX_WORKITEM_ROOT")
    if workitem_root:
        candidates.append(os.path.join(workitem_root, _SKIP_SENTINEL))
    candidates.append(os.path.join(os.getcwd(), _SKIP_SENTINEL))
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path) as fh:
                    reason = fh.read().strip()
            except Exception:
                reason = "(unable to read sentinel file)"
            print("=" * 70, flush=True)
            print("WORK ITEM SKIPPED", flush=True)
            print("=" * 70, flush=True)
            print(f"Sentinel: {path}", flush=True)
            print(f"Reason:   {reason}", flush=True)
            print("Exiting 0 so Helix records this work item as passed.",
                  flush=True)
            print("=" * 70, flush=True)
            sys.exit(0)


if __name__ == "__main__":
    _check_skip_sentinel()
    traits = TestTraits(exename=EXENAME,
                        guiapp='false',
                        )
    Runner(traits).run()
