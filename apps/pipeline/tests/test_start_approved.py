"""Gate 1 as a poll, and the loop that turned out to be hiding in it.

Under Step Functions this was a Supabase Database Webhook on the transition into
`approved`. A transition happens once, so "one production per approval" came
free and nobody had to state it.

A poll has no notion of a transition -- it re-asks the same question every few
seconds -- so the property has to be enforced by the query, and getting it
subtly wrong is expensive rather than merely wrong.
"""

from __future__ import annotations

from tests.conftest import FakeSupa


class TestOneProductionPerApprovedIdea:
    """The regression, and why it is worth this many words.

    The first version of `start_approved_productions` excluded ideas that had a
    *live* production, mirroring the `productions_one_live_per_idea` index --
    which deliberately excludes rejected, failed and parked so that a parked
    production does not permanently block a deliberate re-run.

    Correct for the index. Wrong for a poller: park the production and the idea
    is eligible again on the very next tick, so the loop is park -> reopen ->
    park, several times a second. Running it against a real database opened
    **44 productions for one idea in about two seconds**, and each of those is a
    render submission. On the `ai-presenter` preset, which bills $1-2 per render
    against a pay-as-you-go wallet, that is the wallet gone inside a minute.

    `claim_render_slot` does not save you here and it is worth being clear why:
    it stops two workers submitting for one production, and these were 44
    legitimately distinct productions.

    The fix is in SQL -- `not exists (... where p.idea_id = i.id)`, with no
    status filter -- so these tests pin the behaviour the Python depends on
    rather than the query itself. The query is exercised for real against
    Supabase.
    """

    def test_it_returns_what_the_database_opened(self):
        supa = FakeSupa()
        supa.started = [{"id": "prod-1"}, {"id": "prod-2"}]

        from pipeline.driver import engine

        assert engine.start_due_productions(supa) == ["prod-1", "prod-2"]

    def test_nothing_approved_is_the_quiet_common_case(self):
        from pipeline.driver import engine

        # This runs every few seconds forever. It must cost one round trip and
        # produce no log line when there is nothing to do.
        assert engine.start_due_productions(FakeSupa()) == []

    def test_the_driver_asks_the_database_rather_than_deciding_for_itself(self):
        # The dedup is one statement against Postgres, backed by a unique index.
        # Any filtering done here instead would be a read followed by a write,
        # with a window between them that two workers could both pass through.
        supa = FakeSupa()

        from pipeline.driver import engine

        engine.start_due_productions(supa)

        assert supa.call_names == ["start_approved_productions"]


class TestOnlyTheDispatcherThreadOpensProductions:
    """Every thread could safely call it; there is no reason for all of them to.

    The insert is guarded by `productions_one_live_per_idea`, so a second caller
    is refused rather than duplicating. But four threads asking the same
    question every five seconds is four times the round trips for an answer that
    is almost always "nothing".
    """

    def test_worker_zero_is_the_dispatcher(self):
        import inspect

        from pipeline.driver import worker

        source = inspect.getsource(worker.run_productions)
        assert "dispatcher = index == 0" in source
        assert "if dispatcher:" in source
