"""The domain-neutrality lint: no field's vocabulary may harden into the engine.

The engine validates structure — presence, hashes, counts, ordering, set
membership — and must read identically to a mathematician, a biologist, a
security auditor, a market analyst, or an SRE. This test scans the source for
vocabulary from several fields; any hit means a domain assumption leaked into
code or an error message. Worked field examples belong in profiles/, never in
src/.
"""

import re
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "adv_loop"

# Seeded from several dissimilar fields so no single field's framing can
# quietly become the engine's framing.
DOMAIN_DENYLIST = (
    # mathematics
    "riemann", "zeta", "eigenvalue", "theorem", "lemma", "conjecture", "integer programming",
    # biomedicine
    "genome", "protein", "enzyme", "clinical", "biopsy", "placebo", "tumor", "dosage",
    # finance
    "portfolio", "equity", "hedge", "ticker", "arbitrage", "basis point",
    # software security
    "malware", "botnet", "phishing", "penetration test", "cve-",
    # machine learning
    "gradient descent", "neural network", "tokenizer", "hyperparameter", "benchmark",
)


class NeutralityLintTest(unittest.TestCase):
    def test_engine_source_carries_no_domain_vocabulary(self):
        pattern = re.compile("|".join(re.escape(term) for term in DOMAIN_DENYLIST))
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if pattern.search(line.lower()):
                    offenders.append(f"{path.name}:{line_number}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "Domain vocabulary leaked into the engine — move field examples to profiles/:\n"
            + "\n".join(offenders),
        )

    def test_error_messages_are_structural_not_interpretive(self):
        # Spot-check: every raise in validation code speaks about structure
        # (fields, counts, ids, statuses), never about what a claim means.
        text = (SRC / "engine.py").read_text(encoding="utf-8")
        self.assertNotIn("plausible", text.lower())
        self.assertNotIn("looks correct", text.lower())
        self.assertNotIn("seems", text.lower())


if __name__ == "__main__":
    unittest.main()
