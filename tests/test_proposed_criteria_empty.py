"""An empty `proposed_criteria` list is "nothing proposed" in every planning mode (regression: 2026-09-11 rejection loop)."""

from __future__ import annotations

import unittest

from adv_loop.engine import LoopEngine
from adv_loop.errors import ValidationError


class EmptyProposedCriteria(unittest.TestCase):
    state = {"criteria": [{"text": "lean formalized"}]}

    def test_empty_list_accepted_in_initial_plan(self):
        self.assertEqual(LoopEngine._validate_proposed_criteria([], self.state, "initial_plan"), [])
        self.assertEqual(LoopEngine._validate_proposed_criteria(None, self.state, "initial_plan"), [])

    def test_real_proposal_still_refused_outside_decompose(self):
        with self.assertRaises(ValidationError):
            LoopEngine._validate_proposed_criteria([{"text": "x", "rationale": "y"}], self.state, "initial_plan")

    def test_non_list_refused(self):
        with self.assertRaises(ValidationError):
            LoopEngine._validate_proposed_criteria("nope", self.state, "decompose")
