"""Private candidate evaluation; no route application, persistence or settlement."""
import copy
from contextlib import contextmanager
import time

import prepared_channels as channels
import prepared_execution as execution


def evaluate(config, runtime, route_id, stored_template, observations):
    """Compile against fresh execution evidence without changing caller state.

    expected_execution is context for a future owner-side CAS, not a lock or
    proof that this candidate was applied. The caller must keep existing debt.
    """
    result, _, _ = _evaluate_collected(config, runtime, route_id, stored_template, observations)
    return result


def _evaluate_collected(config, runtime, route_id, stored_template, observations):
    """Private owning path: retain the SAME collected context, never accept one."""
    config, runtime, stored_template, observations, route, plan = _inputs(
        config, runtime, route_id, stored_template, observations)
    view, effective = execution.collect()
    return _stage_collected(config, runtime, route_id, stored_template, observations, route, plan, view, effective)


@contextmanager
def _owning_evaluation(config, runtime, route_id, stored_template, observations):
    """One collector context with its verified descriptors kept open for writes."""
    config, runtime, stored_template, observations, route, plan = _inputs(
        config, runtime, route_id, stored_template, observations)
    with execution._collected_evidence(stable=True) as ((view, effective), _, recheck):
        result, view, effective = _stage_collected(
            config, runtime, route_id, stored_template, observations, route, plan, view, effective)
        yield result, view, effective, recheck


def _inputs(config, runtime, route_id, stored_template, observations):
    if any(not isinstance(value, dict) for value in (config, runtime, stored_template, observations)):
        raise ValueError("complete private evaluation inputs are required")
    config, runtime, stored_template, observations = copy.deepcopy(
        (config, runtime, stored_template, observations))
    routes = [route for route in config.get("routes", []) if route.get("id") == route_id]
    if len(routes) != 1 or not isinstance(routes[0].get("prepared_plan"), dict):
        raise ValueError("one existing prepared Auto Route with a stored plan is required")
    route = routes[0]
    plan = route["prepared_plan"]
    channels.bound_plan(config, route_id, plan)
    if route.get("presentation", {}).get("published"):
        raise ValueError("private evaluation requires an unpublished prepared route")
    if plan["channel"]["engine"] != {"name": "xray", "version": "26.10.21", "sha256": execution.CORE}:
        raise ValueError("evaluation requires the pinned stock engine")

    return config, runtime, stored_template, observations, route, plan


def _stage_collected(config, runtime, route_id, stored_template, observations, route, plan, view, effective):
    view, effective = copy.deepcopy((view, effective))
    now = int(time.time())
    candidate, observed, template, binding = channels.stage(
        config, runtime, route_id, plan, stored_template, observations,
        now=now, runtime_view=view, effective_config=effective)
    update = channels.template_needs_update(template, stored_template, view, effective, route)
    return {
        "candidate": {"config": candidate, "runtime": observed, "template": template},
        "binding": binding,
        "update_required": update,
        "expected_execution": {"generation": view["native_generation"], "epoch": view["mutation_epoch"]},
        "candidate_only": True,
        "applied": False,
        "settled": False,
        "deny_outcome": "not_verified",
        "capability": False,
    }, view, effective
