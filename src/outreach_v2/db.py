from __future__ import annotations
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import (MetaData, Table, Column, String, Text, Integer, Boolean, LargeBinary,
                        ForeignKey, UniqueConstraint, Index, create_engine, event, text, inspect, select)

SCHEMA = 'outreach_v2'
metadata = MetaData(schema=SCHEMA)

def now():
    return datetime.now(timezone.utc).isoformat()

def table(name, *cols, **kw):
    return Table(name, metadata, *cols, **kw)

def col(name, typ=Text, **kw):
    return Column(name, typ, **kw)

candidates = table('candidates', col('id', String, primary_key=True), col('kind', String, nullable=False),
    col('name', nullable=False), col('website'), col('domain', String, index=True), col('industry'), col('location'),
    col('description'), col('website_status', String, default='UNKNOWN'), col('qualification', String, default='UNKNOWN'),
    col('geography', String, default='UNKNOWN'), col('suppressed', Boolean, nullable=False, default=False),
    col('source', String), col('source_ref', String), col('source_checksum', String),
    col('personalization', default='{}'), col('personalization_status', String, default='INCOMPLETE'),
    col('personalization_at'), col('personalization_error'), col('fixture', Boolean, default=False, nullable=False),
    col('created_at', nullable=False), col('updated_at', nullable=False))
contacts = table('contacts', col('id', String, primary_key=True),
    Column('candidate_id', String, ForeignKey(f'{SCHEMA}.candidates.id'), nullable=False, index=True),
    col('kind', String), col('email', String, index=True), col('url'), col('source_url'), col('evidence'),
    col('verification', String), col('purpose'), col('suppressed', Boolean, default=False, nullable=False),
    col('source_ref', String), col('source_checksum', String), col('raw'))
sources = table('sources', col('id', String, primary_key=True), col('path'), col('relation'), col('fingerprint'),
    col('expected', Integer), col('processed', Integer, default=0), col('status'), col('updated_at'))
source_rows = table('source_rows', col('id', String, primary_key=True),
    Column('source_id', String, ForeignKey(f'{SCHEMA}.sources.id'), nullable=False, index=True),
    col('source_key'), Column('candidate_id', String, ForeignKey(f'{SCHEMA}.candidates.id'), index=True),
    col('disposition'), col('reason'), col('checksum'), col('raw'))
source_memberships = table('source_memberships', col('id', String, primary_key=True),
    Column('source_id', String, ForeignKey(f'{SCHEMA}.sources.id'), nullable=False, index=True),
    col('offset', Integer), col('count', Integer), col('payload', LargeBinary))
suppressions = table('suppressions', col('email', String, primary_key=True), col('reason'), col('recorded_at'))
batches = table('batches', col('id', String, primary_key=True), col('request_key', String, unique=True, nullable=False),
    col('selection_hash', String, nullable=False), col('created_at'), col('exported_at'), col('send_status', default='UNVERIFIED'),
    col('send_evidence'), col('send_recorded_at'), col('excluded', default='[]'), col('package', LargeBinary), col('bulk_subject'))
invitations = table('invitations', col('id', String, primary_key=True),
    Column('candidate_id', String, ForeignKey(f'{SCHEMA}.candidates.id'), unique=True, nullable=False),
    Column('batch_id', String, ForeignKey(f'{SCHEMA}.batches.id'), nullable=False, index=True),
    col('reference', String, unique=True, nullable=False), col('recipient', String, nullable=False),
    col('website', nullable=False), col('company_name', nullable=False), col('subject'), col('body'), col('form_url'),
    col('template_version'), col('personalization'), col('generated_at'), col('exported_at'), col('send_status', default='UNVERIFIED'),
    col('consent_status', default='NOT_RECEIVED'), col('authority', default='{}'))
submissions = table('submissions', col('id', String, primary_key=True),
    Column('invitation_id', String, ForeignKey(f'{SCHEMA}.invitations.id'), index=True), col('reference'),
    col('interest'), col('authorization'), col('representative_name'), col('representative_role'), col('representative_email'),
    col('submitted_at'), col('received_at'), col('identity_provider'), col('identity_verified_email'),
    col('identity_reference'), col('status'), col('reason'), col('raw'))
jobs = table('jobs', col('id', String, primary_key=True),
    Column('invitation_id', String, ForeignKey(f'{SCHEMA}.invitations.id'), unique=True, nullable=False),
    Column('submission_id', String, ForeignKey(f'{SCHEMA}.submissions.id'), nullable=False),
    col('status', String, index=True), col('stage'), col('idempotency_key', String, unique=True), col('property_id'),
    col('audit_id'), col('opportunity_ids'), col('engine_job_id'), col('result_id'), col('result'),
    col('attempts', Integer, default=0), col('next_attempt_at'), col('lease_until'), col('lease_token'),
    col('error'), col('created_at'), col('started_at'), col('completed_at'), col('updated_at'))
reports = table('reports', col('id', String, primary_key=True),
    Column('job_id', String, ForeignKey(f'{SCHEMA}.jobs.id'), unique=True, nullable=False),
    col('recipient', String, nullable=False), col('result_id', String, unique=True), col('document', LargeBinary),
    col('sha256'), col('created_at'))
deliveries = table('deliveries', col('id', String, primary_key=True),
    Column('report_id', String, ForeignKey(f'{SCHEMA}.reports.id'), unique=True, nullable=False),
    col('recipient', nullable=False), col('provider'), col('idempotency_key', String, unique=True),
    col('status', String), col('provider_message_id'), col('attempted_at'), col('accepted_at'), col('delivered_at'),
    col('error'), col('next_attempt_at'), col('attempts', Integer, default=0))
events = table('events', col('id', String, primary_key=True), col('entity_id', String, index=True),
    col('event'), col('detail'), col('created_at'))
sessions = table('sessions', col('token_hash', String, primary_key=True), col('csrf_hash'), col('email'),
    col('expires_at'), col('created_at'))
auth_attempts = table('auth_attempts', col('id', String, primary_key=True), col('source', String, index=True),
    col('successful', Boolean), col('created_at'))
worker_runs = table('worker_runs', col('id', String, primary_key=True), col('started_at'), col('finished_at'),
    col('status'), col('detail'))
schema_migrations = table('schema_migrations', Column('version', Integer, primary_key=True),
    col('name', String, nullable=False), col('applied_at', nullable=False))
Index('candidate_filter', candidates.c.kind, candidates.c.website_status, candidates.c.qualification)

MIGRATIONS = [(1, 'verified-respondent-consent-evidence', {
    'submissions': {'identity_provider': 'TEXT', 'identity_verified_email': 'TEXT', 'identity_reference': 'TEXT'}
}), (2, 'lean-operational-evidence-references', {
    'candidates': {'source_ref': 'TEXT', 'source_checksum': 'TEXT'},
    'contacts': {'source_ref': 'TEXT', 'source_checksum': 'TEXT'}
})]

class Database:
    def __init__(self, url=None):
        url = url or os.environ.get('OUTREACH_V2_DATABASE_URL') or os.environ.get('OUTREACH_DATABASE_URL')
        if not url:
            raise RuntimeError('OUTREACH_V2_DATABASE_URL is required; no implicit local fallback')
        url = url.replace('postgres://','postgresql+psycopg://',1).replace('postgresql://','postgresql+psycopg://',1)
        self.postgres = url.startswith('postgresql')
        options = {'connect_timeout': 10} if self.postgres else {'check_same_thread': False, 'timeout': 30}
        self.engine = create_engine(url, connect_args=options, pool_pre_ping=True,
                                    execution_options={'schema_translate_map': {SCHEMA: SCHEMA if self.postgres else None}})
        if not self.postgres:
            @event.listens_for(self.engine, 'connect')
            def configure(dbapi, _):
                dbapi.execute('PRAGMA foreign_keys=ON')
                dbapi.execute('PRAGMA journal_mode=WAL')
    def initialize(self):
        with self.engine.begin() as c:
            if self.postgres:
                c.execute(text(f'CREATE SCHEMA IF NOT EXISTS {SCHEMA}'))
            metadata.create_all(c)
            if self.postgres:
                c.execute(text(f'LOCK TABLE {SCHEMA}.schema_migrations IN EXCLUSIVE MODE'))
            for version, name, changes in MIGRATIONS:
                if c.execute(select(schema_migrations.c.version).where(schema_migrations.c.version == version)).first():
                    continue
                for table_name, columns in changes.items():
                    existing = {column['name'] for column in inspect(c).get_columns(table_name, schema=SCHEMA if self.postgres else None)}
                    for column_name, sql_type in columns.items():
                        if column_name not in existing:
                            schema = f'{SCHEMA}.' if self.postgres else ''
                            c.execute(text(f'ALTER TABLE {schema}{table_name} ADD COLUMN {column_name} {sql_type}'))
                c.execute(schema_migrations.insert().values(version=version, name=name, applied_at=now()))
    @contextmanager
    def transaction(self, *, serialized=False):
        with self.engine.connect() as c:
            try:
                if serialized:
                    if self.postgres:
                        c.execute(text('SELECT pg_advisory_xact_lock(824761932)'))
                    else:
                        c.exec_driver_sql('BEGIN IMMEDIATE')
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise
    def upsert(self, c, relation, rows, keys=('id',), update=True):
        if not rows: return
        if self.postgres:
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        statement = insert(relation)
        if update:
            statement = statement.on_conflict_do_update(index_elements=list(keys),
                set_={x.name: getattr(statement.excluded,x.name) for x in relation.columns if x.name not in keys})
        else:
            statement = statement.on_conflict_do_nothing(index_elements=list(keys))
        c.execute(statement, rows)
