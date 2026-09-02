"""Small exhaustive rational oracle. No candidate or numerical-helper imports.

This evaluates specified finite laws, not estimated population probabilities.
All golden answers live in the separately frozen fixture file.
"""

from __future__ import annotations

from fractions import Fraction


class FiniteLaw:
    def __init__(self, atoms):
        merged = {}
        for value, mass in atoms:
            value, mass = Fraction(value), Fraction(mass)
            if mass < 0:
                raise ValueError("negative probability mass")
            merged[value] = merged.get(value, Fraction(0)) + mass
        total = sum(merged.values(), Fraction(0))
        if total <= 0:
            raise ValueError("positive total mass required")
        self.atoms = tuple(sorted((value, mass / total) for value, mass in merged.items() if mass))

    def mean(self):
        return sum((value * mass for value, mass in self.atoms), Fraction(0))

    def quantile(self, probability):
        probability = Fraction(probability)
        if not 0 <= probability <= 1:
            raise ValueError("probability outside [0, 1]")
        cumulative = Fraction(0)
        for value, mass in self.atoms:
            cumulative += mass
            if cumulative >= probability:
                return value
        raise AssertionError("normalized law must exhaust probability")

    def cvar(self, confidence):
        confidence = Fraction(confidence)
        if not 0 < confidence < 1:
            raise ValueError("confidence outside (0, 1)")
        remaining = 1 - confidence
        integral = Fraction(0)
        for value, mass in reversed(self.atoms):
            included = min(remaining, mass)
            integral += included * value
            remaining -= included
            if not remaining:
                break
        return integral / (1 - confidence)

    def breach_probability(self, limit):
        limit = Fraction(limit)
        return sum((mass for value, mass in self.atoms if value > limit), Fraction(0))


def choose_action(actions, risk_limit):
    """Exhaustive feasible cost comparison; hold wins an equal-cost tie."""
    feasible = [
        (name, Fraction(cost))
        for name, cost, risk in actions
        if Fraction(risk) <= Fraction(risk_limit)
    ]
    if not feasible:
        return "collect_evidence"
    return min(feasible, key=lambda item: (item[1], item[0] != "hold", item[0]))[0]


def evaluate_fixture(fixture):
    """Evaluate inputs without reading the fixture's expected-answer field."""
    if "candidate_present" in fixture and (
        not fixture["candidate_present"] or not fixture["calibration_present"]
    ):
        return {"choice": "collect_evidence", "distribution": None}
    if "losses" in fixture:
        law = FiniteLaw([(value, 1) for value in fixture["losses"]])
        return {
            "var": str(law.quantile(fixture["confidence"])),
            "cvar": str(law.cvar(fixture["confidence"])),
        }
    if "actions" in fixture:
        return {"choice": choose_action(fixture["actions"], fixture["risk_limit"])}
    if "breaches" in fixture:
        probability = Fraction(fixture["breaches"], fixture["scenarios"])
        answer = {"probability": str(probability)}
        if "risk_limit" in fixture:
            answer["feasible"] = probability <= Fraction(fixture["risk_limit"])
        return answer
    incumbent = Fraction(fixture["incumbent_cost"])
    candidate = Fraction(fixture["candidate_cost"])
    demand = FiniteLaw(fixture["demand"])
    savings = FiniteLaw([(volume * (incumbent - candidate), mass) for volume, mass in demand.atoms])
    penalty = Fraction(fixture.get("penalty", "0"))
    errors = Fraction(fixture.get("incremental_errors", "0"))
    answer = {"savings": str(savings.mean())}
    if len(demand.atoms) > 1:
        answer["savings_law"] = [[str(value), str(mass)] for value, mass in savings.atoms]
    else:
        answer["loss"] = str(-savings.mean() + penalty * errors)
        if incumbent == candidate:
            answer["choice"] = "hold"
        elif not penalty:
            answer["cost"] = str(demand.mean() * candidate)
    return answer
