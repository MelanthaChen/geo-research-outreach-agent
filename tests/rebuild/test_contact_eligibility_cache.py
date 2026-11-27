import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import exists,func,select

from outreach_v2.db import (Database,candidates,contacts,suppressions,now,
    contact_eligibility_cache,contact_eligibility_dirty,contact_eligibility_state)
from outreach_v2.service import (authorized_candidate_ids_for_scope,
    backfill_contact_eligibility_cache,invitation_contacts)


@pytest.fixture
def db(tmp_path):
    instance=Database('sqlite:///'+str(tmp_path/'eligibility-cache.sqlite'))
    instance.initialize()
    return instance


def seed(db,ident):
    website=f'https://{ident}.org'
    email=f'office@{ident}.org'
    with db.transaction() as c:
        c.execute(candidates.insert().values(id=ident,kind='company',name=ident,website=website,
            domain=f'{ident}.org',website_status='VERIFIED',qualification='ELIGIBLE',
            suppressed=False,fixture=False,created_at=now(),updated_at=now()))
        c.execute(contacts.insert().values(id='contact-'+ident,candidate_id=ident,kind='EMAIL',
            email=email,source_url=website+'/contact',purpose='general business',
            evidence=f'For general business inquiries contact {email}',verification='UNKNOWN',
            suppressed=False))


def assert_authorized(db,ident,expected):
    with db.transaction() as c:
        assert (ident in authorized_candidate_ids_for_scope(c)) is expected


def test_projection_backfill_and_contact_candidate_suppression_invalidation(db):
    ident='cache-'+uuid.uuid4().hex[:12]
    seed(db,ident)
    assert backfill_contact_eligibility_cache(db)==1
    assert_authorized(db,ident,True)

    # Contact evidence edits invalidate the owning candidate atomically.
    with db.transaction() as c:
        c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(evidence='Mailbox only'))
        assert c.scalar(select(func.count()).select_from(contact_eligibility_dirty)
            .where(contact_eligibility_dirty.c.candidate_id==ident))==1
    assert_authorized(db,ident,False)
    with db.transaction() as c:
        c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(
            evidence=f'For general business inquiries contact office@{ident}.org'))
    assert_authorized(db,ident,True)
    with db.transaction() as c:
        c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(kind='PRESS'))
    assert_authorized(db,ident,False)
    with db.transaction() as c:
        c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(kind='GENERAL_BUSINESS_EMAIL'))
    assert_authorized(db,ident,True)

    # Suppressions invalidate all contacts for that exact address; removing the
    # suppression restores eligibility after the queued candidate is refreshed.
    with db.transaction() as c:
        c.execute(suppressions.insert().values(email=f'OFFICE@{ident}.org',reason='test',recorded_at=now()))
        # Invitation selection remains authoritative while the read projection
        # is merely marked dirty and has not yet been refreshed.
        live=select(contacts.c.id).select_from(contacts.join(candidates,
            candidates.c.id==contacts.c.candidate_id)).where(candidates.c.id==ident,invitation_contacts())
        assert c.scalar(select(exists(live))) is False
    assert_authorized(db,ident,False)
    with db.transaction() as c:
        c.execute(suppressions.delete().where(func.lower(suppressions.c.email)==f'office@{ident}.org'))
    assert_authorized(db,ident,True)

    # Candidate website/domain provenance is also a cache dependency.
    with db.transaction() as c:
        c.execute(candidates.update().where(candidates.c.id==ident).values(domain='changed.invalid'))
    assert_authorized(db,ident,False)
    with db.transaction() as c:
        c.execute(candidates.update().where(candidates.c.id==ident).values(domain=f'{ident}.org'))
    assert_authorized(db,ident,True)

    # Invitation safety continues to use the live authoritative expression.
    assert 'outreach_v2.contacts' in str(invitation_contacts())


def test_projection_uninitialized_uses_live_sql_until_backfill(db):
    ident='uncached-'+uuid.uuid4().hex[:12]
    seed(db,ident)
    with db.transaction() as c:
        assert c.scalar(select(contact_eligibility_state.c.initialized).where(
            contact_eligibility_state.c.id==1)) is False
    assert_authorized(db,ident,True)
    assert backfill_contact_eligibility_cache(db)==1
    with db.transaction() as c:
        assert c.scalar(select(contact_eligibility_cache.c.candidate_id).where(
            contact_eligibility_cache.c.candidate_id==ident))==ident


def test_missing_state_and_interrupted_backfill_fall_back_without_partial_cache(db):
    from sqlalchemy import event
    ident='partial-'+uuid.uuid4().hex[:12]
    seed(db,ident);assert backfill_contact_eligibility_cache(db)==1
    with db.transaction() as c:
        c.execute(contact_eligibility_state.delete().where(contact_eligibility_state.c.id==1))
    # Missing state is treated as uninitialized and uses the authoritative SQL.
    assert_authorized(db,ident,True)
    db.initialize()  # Idempotent migration restores the false state row.
    assert_authorized(db,ident,True)

    # Inject a transaction failure after the cache has been rewritten but
    # before dirty rows/state can commit. PostgreSQL/SQLite roll the entire
    # repair back; the subsequent reader drains the durable dirty row.
    with db.transaction() as c:
        c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(evidence='No longer published'))
    def interrupt(conn,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('UPDATE') and 'contact_eligibility_state' in statement:
            raise RuntimeError('simulated interruption during projection repair')
    event.listen(db.engine,'before_cursor_execute',interrupt)
    try:
        with pytest.raises(RuntimeError,match='simulated interruption'):
            backfill_contact_eligibility_cache(db)
    finally:event.remove(db.engine,'before_cursor_execute',interrupt)
    with db.transaction() as c:
        assert c.scalar(select(contact_eligibility_state.c.initialized).where(
            contact_eligibility_state.c.id==1)) is False
        assert c.scalar(select(func.count()).select_from(contact_eligibility_dirty).where(
            contact_eligibility_dirty.c.candidate_id==ident))==1
    # The interrupted transaction leaves the projection explicitly
    # uninitialized, so reads use the authoritative SQL until retry succeeds.
    assert_authorized(db,ident,False)


def test_contact_rollback_does_not_leave_dirty_or_change_projection(db):
    ident='rollback-'+uuid.uuid4().hex[:12]
    seed(db,ident);backfill_contact_eligibility_cache(db)
    with pytest.raises(RuntimeError,match='abort contact transaction'):
        with db.transaction() as c:
            c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(evidence='No longer published'))
            raise RuntimeError('abort contact transaction')
    assert_authorized(db,ident,True)
    with db.transaction() as c:
        assert c.scalar(select(func.count()).select_from(contact_eligibility_dirty).where(
            contact_eligibility_dirty.c.candidate_id==ident))==0


def test_postgres_projection_triggers_invalidate_exact_candidate_state():
    url=os.environ.get('OUTREACH_TEST_POSTGRES_URL')
    if not url:pytest.skip('OUTREACH_TEST_POSTGRES_URL must point at a disposable local PostgreSQL database')
    db=Database(url);db.initialize();db.initialize();ident='cache-pg-'+uuid.uuid4().hex[:12]
    try:
        seed(db,ident)
        backfill_contact_eligibility_cache(db)
        assert_authorized(db,ident,True)
        with db.transaction() as c:
            c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(kind='PRESS'))
        assert_authorized(db,ident,False)
        with db.transaction() as c:
            c.execute(contacts.update().where(contacts.c.candidate_id==ident).values(kind='GENERAL_BUSINESS_EMAIL'))
        assert_authorized(db,ident,True)
        with db.transaction() as c:
            c.execute(suppressions.insert().values(email=f'OFFICE@{ident}.org',reason='test',recorded_at=now()))
        assert_authorized(db,ident,False)
        with db.transaction() as c:
            c.execute(suppressions.delete().where(func.lower(suppressions.c.email)==f'office@{ident}.org'))
        assert_authorized(db,ident,True)
        # Multiple matching contact rows must not cause an ON CONFLICT
        # cardinality error while a suppression queues a candidate once.
        with db.transaction() as c:
            c.execute(contacts.insert().values(id='duplicate-'+ident,candidate_id=ident,kind='EMAIL',
                email=f'office@{ident}.org',source_url=f'https://{ident}.org/contact',
                purpose='general business',evidence=f'For general business inquiries contact office@{ident}.org',
                verification='UNKNOWN',suppressed=False))
            c.execute(suppressions.insert().values(email=f'OFFICE@{ident}.org',reason='duplicate test',recorded_at=now()))
        assert_authorized(db,ident,False)
        with db.transaction() as c:
            c.execute(suppressions.delete().where(func.lower(suppressions.c.email)==f'office@{ident}.org'))
        assert_authorized(db,ident,True)
    finally:
        with db.transaction() as c:
            c.execute(suppressions.delete().where(func.lower(suppressions.c.email)==f'office@{ident}.org'))
            c.execute(contacts.delete().where(contacts.c.candidate_id==ident))
            c.execute(candidates.delete().where(candidates.c.id==ident))
            c.execute(contact_eligibility_cache.delete().where(contact_eligibility_cache.c.candidate_id==ident))
            c.execute(contact_eligibility_dirty.delete().where(contact_eligibility_dirty.c.candidate_id==ident))
        db.engine.dispose()


def test_postgres_concurrent_reader_writer_and_dirty_refresh_are_serialized():
    url=os.environ.get('OUTREACH_TEST_POSTGRES_URL')
    if not url:pytest.skip('OUTREACH_TEST_POSTGRES_URL must point at a disposable local PostgreSQL database')
    db=Database(url);db.initialize();ident='cache-race-'+uuid.uuid4().hex[:12]
    seed(db,ident);backfill_contact_eligibility_cache(db)
    first_reader_holds_lock=threading.Event();release_first_reader=threading.Event()
    writer_started=threading.Event();second_reader_started=threading.Event()
    def first_reader():
        with db.transaction() as c:
            before=ident in authorized_candidate_ids_for_scope(c)
            first_reader_holds_lock.set()
            assert release_first_reader.wait(5)
            return before
    def suppress():
        assert first_reader_holds_lock.wait(5)
        with db.transaction() as c:
            writer_started.set()
            c.execute(suppressions.insert().values(email=f'office@{ident}.org',reason='race test',recorded_at=now()))
    def second_reader():
        second_reader_started.set()
        with db.transaction() as c:return ident in authorized_candidate_ids_for_scope(c)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first=pool.submit(first_reader)
            assert first_reader_holds_lock.wait(5)
            writer=pool.submit(suppress)
            assert writer_started.wait(5)
            time.sleep(.1)
            assert not writer.done()  # Trigger waits on the projection lock.
            release_first_reader.set()
            assert first.result(timeout=5) is True  # Snapshot predates committed suppression.
            writer.result(timeout=5)
            # Concurrent readers serialize behind the same refresh lock. Both
            # observe the committed suppression after the dirty row is drained.
            first_refresh=pool.submit(second_reader)
            assert second_reader_started.wait(5)
            second=pool.submit(second_reader)
            assert first_refresh.result(timeout=5) is False
            assert second.result(timeout=5) is False
    finally:
        release_first_reader.set()
        with db.transaction() as c:
            c.execute(suppressions.delete().where(func.lower(suppressions.c.email)==f'office@{ident}.org'))
            c.execute(contacts.delete().where(contacts.c.candidate_id==ident))
            c.execute(candidates.delete().where(candidates.c.id==ident))
            c.execute(contact_eligibility_cache.delete().where(contact_eligibility_cache.c.candidate_id==ident))
            c.execute(contact_eligibility_dirty.delete().where(contact_eligibility_dirty.c.candidate_id==ident))
        db.engine.dispose()
