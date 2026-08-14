import operator


class TestEvaluator:
    OPERATORS = {
        "==": operator.eq, "!=": operator.ne, ">": operator.gt,
        ">=": operator.ge, "<": operator.lt, "<=": operator.le,
    }

    def evaluate(self, measurements, rules):
        results = []
        for rule in rules:
            metric, operation, expected = rule["metric"], rule["operator"], rule["expected"]
            actual = measurements.get(metric)
            try:
                passed = actual is not None and self.OPERATORS[operation](actual, expected)
            except (TypeError, ValueError):
                passed = False
            results.append({"metric": metric, "actual": actual, "operator": operation, "expected": expected, "passed": passed})
        return results
