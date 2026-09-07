"""The production driver.

Replaces AWS Step Functions with a loop over rows in Postgres. `graph.py` is the
state machine's shape, `engine.py` runs one step of it, `sweeps.py` is what
EventBridge used to schedule, and `worker.py` is the process.
"""
