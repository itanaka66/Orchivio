"""initial platform schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-17 10:50:23.897547
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# JSONB on PostgreSQL; a JSON array of strings elsewhere (see orchivio.db.models.UuidList).
JSON_T = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID_LIST = sa.JSON().with_variant(postgresql.ARRAY(sa.Uuid()), "postgresql")

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _add_postgres_extras() -> None:
    """Constraints SQLite cannot add after table creation, plus immutability triggers."""
    # Circular references (applications/branches <-> app_revisions) are added once both exist.
    # SQLite cannot ALTER TABLE ADD CONSTRAINT, so the unit-test schema omits these two FKs.
    if not _is_postgres():
        return
    op.create_foreign_key(
        "fk_applications_current_revision", "applications", "app_revisions",
        ["current_revision_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_branches_head_revision", "branches", "app_revisions", ["head_revision_id"], ["id"]
    )
    op.execute(
        """
        CREATE FUNCTION orchivio_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% on % is not allowed (append-only table)', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    # Revisions are immutable; DELETE is left to cascades from project deletion/retention.
    op.execute(
        "CREATE TRIGGER app_revisions_immutable BEFORE UPDATE ON app_revisions "
        "FOR EACH ROW EXECUTE FUNCTION orchivio_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION orchivio_reject_change()"
    )


def _drop_postgres_extras() -> None:
    if not _is_postgres():
        return
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS app_revisions_immutable ON app_revisions")
    op.execute("DROP FUNCTION IF EXISTS orchivio_reject_change()")
    op.drop_constraint("fk_branches_head_revision", "branches", type_="foreignkey")
    op.drop_constraint("fk_applications_current_revision", "applications", type_="foreignkey")


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=True),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('plane', sa.String(length=16), nullable=False),
    sa.Column('channel', sa.String(length=8), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('target_type', sa.String(length=64), nullable=True),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('metadata', JSON_T, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("channel IN ('studio', 'api', 'mcp')", name=op.f('ck_audit_events_channel')),
    sa.CheckConstraint("plane IN ('platform', 'application')", name=op.f('ck_audit_events_plane')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_index('ix_audit_events_org_time', 'audit_events', ['organization_id', 'created_at'], unique=False)
    op.create_index('ix_audit_events_target', 'audit_events', ['target_type', 'target_id'], unique=False)
    op.create_table('organizations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=63), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    sa.UniqueConstraint('slug', name=op.f('uq_organizations_slug'))
    )
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f('ck_users_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    # Emails are unique case-insensitively.
    op.create_index('uq_users_email_lower', 'users', [sa.text('lower(email)')], unique=True)
    op.create_table('memberships',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("role IN ('owner', 'admin', 'editor', 'viewer')", name=op.f('ck_memberships_role')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_memberships_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_memberships_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('organization_id', 'user_id', name=op.f('pk_memberships'))
    )
    op.create_index(op.f('ix_memberships_user_id'), 'memberships', ['user_id'], unique=False)
    op.create_table('projects',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=63), nullable=False),
    sa.Column('settings', JSON_T, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_projects_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
    sa.UniqueConstraint('organization_id', 'slug', name='uq_projects_org_slug')
    )
    op.create_index(op.f('ix_projects_organization_id'), 'projects', ['organization_id'], unique=False)
    op.create_table('ai_conversations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_ai_conversations_created_by_users')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_ai_conversations_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_conversations'))
    )
    op.create_index(op.f('ix_ai_conversations_project_id'), 'ai_conversations', ['project_id'], unique=False)
    op.create_table('api_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=True),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('token_hash', sa.String(length=128), nullable=False),
    sa.Column('scopes', JSON_T, nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_api_tokens_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_api_tokens_owner_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_api_tokens_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_api_tokens_token_hash'))
    )
    op.create_index(op.f('ix_api_tokens_owner_id'), 'api_tokens', ['owner_id'], unique=False)
    op.create_table('applications',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('key', sa.String(length=63), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('current_revision_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_applications_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_applications')),
    sa.UniqueConstraint('project_id', 'key', name='uq_applications_project_key')
    )
    op.create_index(op.f('ix_applications_project_id'), 'applications', ['project_id'], unique=False)
    op.create_table('ai_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=True),
    sa.Column('conversation_id', sa.Uuid(), nullable=True),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('input_ref', sa.String(length=500), nullable=True),
    sa.Column('result_ref', sa.String(length=500), nullable=True),
    sa.Column('provider', sa.String(length=64), nullable=True),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('prompt_template_version', sa.String(length=64), nullable=True),
    sa.Column('usage', JSON_T, nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name=op.f('ck_ai_jobs_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_ai_jobs_application_id_applications')),
    sa.ForeignKeyConstraint(['conversation_id'], ['ai_conversations.id'], name=op.f('fk_ai_jobs_conversation_id_ai_conversations')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_ai_jobs_created_by_users')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_ai_jobs_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_jobs'))
    )
    op.create_index(op.f('ix_ai_jobs_project_id'), 'ai_jobs', ['project_id'], unique=False)
    op.create_table('ai_messages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('metadata', JSON_T, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("role IN ('user', 'assistant', 'tool', 'system')", name=op.f('ck_ai_messages_role')),
    sa.ForeignKeyConstraint(['conversation_id'], ['ai_conversations.id'], name=op.f('fk_ai_messages_conversation_id_ai_conversations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_messages'))
    )
    op.create_index(op.f('ix_ai_messages_conversation_id'), 'ai_messages', ['conversation_id'], unique=False)
    op.create_table('branches',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('head_revision_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('open', 'merged', 'abandoned')", name=op.f('ck_branches_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_branches_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_branches')),
    sa.UniqueConstraint('application_id', 'name', name='uq_branches_app_name')
    )
    op.create_index(op.f('ix_branches_application_id'), 'branches', ['application_id'], unique=False)
    op.create_table('deployments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('environment', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('artifact_ref', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name=op.f('ck_deployments_status')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_deployments_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployments'))
    )
    op.create_index(op.f('ix_deployments_application_id'), 'deployments', ['application_id'], unique=False)
    op.create_table('app_revisions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('branch_id', sa.Uuid(), nullable=True),
    sa.Column('revision_no', sa.Integer(), nullable=False),
    sa.Column('parent_revision_ids', UUID_LIST, nullable=False),
    sa.Column('schema_version', sa.String(length=16), nullable=False),
    sa.Column('model_json', JSON_T, nullable=False),
    sa.Column('model_hash', sa.String(length=80), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_app_revisions_application_id_applications'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], name=op.f('fk_app_revisions_branch_id_branches')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_app_revisions_created_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_revisions')),
    sa.UniqueConstraint('application_id', 'revision_no', name='uq_app_revisions_app_no')
    )
    op.create_index('ix_app_revisions_branch', 'app_revisions', ['branch_id', 'revision_no'], unique=False)
    _add_postgres_extras()
    op.create_table('builds',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('artifact_ref', sa.String(length=500), nullable=True),
    sa.Column('logs_ref', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name=op.f('ck_builds_status')),
    sa.ForeignKeyConstraint(['revision_id'], ['app_revisions.id'], name=op.f('fk_builds_revision_id_app_revisions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_builds'))
    )
    op.create_index(op.f('ix_builds_revision_id'), 'builds', ['revision_id'], unique=False)
    op.create_table('element_refs',
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('from_element_id', sa.Uuid(), nullable=False),
    sa.Column('to_element_id', sa.Uuid(), nullable=False),
    sa.Column('ref_kind', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['revision_id'], ['app_revisions.id'], name=op.f('fk_element_refs_revision_id_app_revisions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('revision_id', 'from_element_id', 'to_element_id', 'ref_kind', name=op.f('pk_element_refs'))
    )
    op.create_index(op.f('ix_element_refs_to_element_id'), 'element_refs', ['to_element_id'], unique=False)
    op.create_table('lint_findings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('rule_id', sa.String(length=64), nullable=False),
    sa.Column('severity', sa.String(length=8), nullable=False),
    sa.Column('element_id', sa.Uuid(), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('suppressed', sa.Boolean(), nullable=False),
    sa.Column('justification', sa.Text(), nullable=True),
    sa.CheckConstraint("severity IN ('error', 'warning', 'info')", name=op.f('ck_lint_findings_severity')),
    sa.ForeignKeyConstraint(['revision_id'], ['app_revisions.id'], name=op.f('fk_lint_findings_revision_id_app_revisions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lint_findings'))
    )
    op.create_index(op.f('ix_lint_findings_revision_id'), 'lint_findings', ['revision_id'], unique=False)
    op.create_table('proposals',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('ai_job_id', sa.Uuid(), nullable=True),
    sa.Column('base_revision_id', sa.Uuid(), nullable=False),
    sa.Column('result_revision_id', sa.Uuid(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('change_set_json', JSON_T, nullable=False),
    sa.Column('approval_level', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('approved_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("source IN ('ai_builder', 'mcp', 'human')", name=op.f('ck_proposals_source')),
    sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'applied', 'superseded')", name=op.f('ck_proposals_status')),
    sa.CheckConstraint('approval_level BETWEEN 0 AND 4', name=op.f('ck_proposals_approval_level')),
    sa.ForeignKeyConstraint(['ai_job_id'], ['ai_jobs.id'], name=op.f('fk_proposals_ai_job_id_ai_jobs')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_proposals_application_id_applications'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_proposals_approved_by_users')),
    sa.ForeignKeyConstraint(['base_revision_id'], ['app_revisions.id'], name=op.f('fk_proposals_base_revision_id_app_revisions')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_proposals_created_by_users')),
    sa.ForeignKeyConstraint(['result_revision_id'], ['app_revisions.id'], name=op.f('fk_proposals_result_revision_id_app_revisions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_proposals'))
    )
    op.create_index(op.f('ix_proposals_application_id'), 'proposals', ['application_id'], unique=False)
    op.create_table('test_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('build_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('summary', JSON_T, nullable=False),
    sa.Column('report_ref', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name=op.f('ck_test_runs_status')),
    sa.ForeignKeyConstraint(['build_id'], ['builds.id'], name=op.f('fk_test_runs_build_id_builds'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_test_runs'))
    )
    op.create_index(op.f('ix_test_runs_build_id'), 'test_runs', ['build_id'], unique=False)


def downgrade() -> None:
    _drop_postgres_extras()
    op.drop_index(op.f('ix_test_runs_build_id'), table_name='test_runs')
    op.drop_table('test_runs')
    op.drop_index(op.f('ix_proposals_application_id'), table_name='proposals')
    op.drop_table('proposals')
    op.drop_index(op.f('ix_lint_findings_revision_id'), table_name='lint_findings')
    op.drop_table('lint_findings')
    op.drop_index(op.f('ix_element_refs_to_element_id'), table_name='element_refs')
    op.drop_table('element_refs')
    op.drop_index(op.f('ix_builds_revision_id'), table_name='builds')
    op.drop_table('builds')
    op.drop_index('ix_app_revisions_branch', table_name='app_revisions')
    op.drop_table('app_revisions')
    op.drop_index(op.f('ix_deployments_application_id'), table_name='deployments')
    op.drop_table('deployments')
    op.drop_index(op.f('ix_branches_application_id'), table_name='branches')
    op.drop_table('branches')
    op.drop_index(op.f('ix_ai_messages_conversation_id'), table_name='ai_messages')
    op.drop_table('ai_messages')
    op.drop_index(op.f('ix_ai_jobs_project_id'), table_name='ai_jobs')
    op.drop_table('ai_jobs')
    op.drop_index(op.f('ix_applications_project_id'), table_name='applications')
    op.drop_table('applications')
    op.drop_index(op.f('ix_api_tokens_owner_id'), table_name='api_tokens')
    op.drop_table('api_tokens')
    op.drop_index(op.f('ix_ai_conversations_project_id'), table_name='ai_conversations')
    op.drop_table('ai_conversations')
    op.drop_index(op.f('ix_projects_organization_id'), table_name='projects')
    op.drop_table('projects')
    op.drop_index(op.f('ix_memberships_user_id'), table_name='memberships')
    op.drop_table('memberships')
    op.drop_table('users')
    op.drop_table('organizations')
    op.drop_index('ix_audit_events_target', table_name='audit_events')
    op.drop_index('ix_audit_events_org_time', table_name='audit_events')
    op.drop_table('audit_events')
