"""Run the existing policy specification against Dispatch's independent engine."""
from test_release_gate import ReleaseGateTests as Specification, TODAY
from fulfillment.rules import release_gate


class TestDispatchRules(Specification):
    def build(self, rows, **kwargs):
        return release_gate.evaluate_release_gate(rows, today=TODAY, **kwargs)


del Specification  # avoid collecting the imported base class twice
