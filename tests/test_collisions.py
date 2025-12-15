from jfo.core.plan import Plan
from jfo.core.operations import Operation, OperationKind


def test_collision_detection_on_dst():
    plan = Plan(title="t")
    plan.extend([
        Operation(kind=OperationKind.MOVE, src="/a", dst="/x"),
        Operation(kind=OperationKind.MOVE, src="/b", dst="/x"),
    ])
    plan.apply_collision_warnings()
    assert plan.operations[0].warning
    assert plan.operations[1].warning
