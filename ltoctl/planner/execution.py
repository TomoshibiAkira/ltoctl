"""Compatibility exports for persisted plan execution."""

from .executor import PlanApplyResult, PlanExecutor, PlanSourceDriftError, apply_plan

__all__ = ["PlanApplyResult", "PlanExecutor", "PlanSourceDriftError", "apply_plan"]

