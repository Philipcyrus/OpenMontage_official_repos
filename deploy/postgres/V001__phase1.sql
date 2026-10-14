-- OpenMontage / Panda AI — Phase 1 durable job history
-- PostgreSQL 14+
--
-- This migration deliberately stores only data that the current launcher and
-- project folders can supply. It does NOT yet model users, Dify conversations,
-- multi-job projects, semantic memory, raw Claude messages, or raw MCP payloads.
-- Those require upstream identity/contracts that do not exist today.
--
-- Security model:
--   * The migration owner is never a runtime login.
--   * om_phase1_writer is a NOLOGIN group role granted to a separate-OS-user
--     ingestion service. The launcher/Claude process must not receive its DSN.
--   * Audit rows are append-only and protected by both privileges and triggers.
--   * Jobs are current-state projections and have column-limited UPDATE rights.
--   * Approval rows describe their provenance. Phase 1 does not claim to prove
--     that an authenticated human approved anything.
--
-- Privacy model:
--   * No raw transcript, tool result, prompt, stdout/stderr, image, audio, video,
--     base64, API credential, request header, or environment dump is stored here.
--   * Transcript archives are represented by metadata and encrypted object URIs.
--   * All JSON supplied to this schema must be redacted before insertion.
--
-- Import/idempotency model:
--   * ingestion_records deduplicates physical source records/snapshots.
--   * Every domain table also has a domain-specific uniqueness rule.
--   * Rewritten cost reports are observations, not additive events. Views select
--     the newest observation for each stable domain key before calculating totals.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS openmontage;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'om_phase1_writer') THEN
        CREATE ROLE om_phase1_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'om_phase1_reader') THEN
        CREATE ROLE om_phase1_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

SET search_path TO openmontage, public;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only audit table % cannot be updated or deleted', TG_TABLE_NAME
        USING ERRCODE = '55000';
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Current job projection
-- ---------------------------------------------------------------------------

CREATE TABLE jobs (
    job_id                     text PRIMARY KEY,
    pipeline                   text NOT NULL,
    profile                    text,
    brief                      text NOT NULL,
    options_redacted           jsonb NOT NULL DEFAULT '{}'::jsonb,
    inputs_redacted            jsonb NOT NULL DEFAULT '[]'::jsonb,
    current_status             text NOT NULL,
    current_stage              text,
    current_gate               text,
    current_question           text,
    state_version              bigint NOT NULL DEFAULT 0,
    code_branch                text,
    code_commit                text,
    runner_version             text,
    processing_started_at      timestamptz,
    processing_finished_at     timestamptz,
    completed_at               timestamptz,
    failed_at                  timestamptz,
    terminal_error_redacted    jsonb,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    CHECK (job_id ~ '^job_[A-Za-z0-9_-]+$'),
    CHECK (jsonb_typeof(options_redacted) = 'object'),
    CHECK (jsonb_typeof(inputs_redacted) = 'array')
);

CREATE INDEX jobs_status_updated_idx ON jobs (current_status, updated_at DESC);
CREATE INDEX jobs_pipeline_updated_idx ON jobs (pipeline, updated_at DESC);

CREATE TRIGGER jobs_set_updated_at
BEFORE UPDATE ON jobs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 2–3. Source files and source records
-- ---------------------------------------------------------------------------

CREATE TABLE ingestion_sources (
    ingestion_source_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    source_kind                text NOT NULL,
    canonical_path             text NOT NULL,
    file_identity              text NOT NULL,
    first_seen_at              timestamptz NOT NULL DEFAULT now(),
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, source_kind, canonical_path, file_identity),
    CHECK (source_kind IN (
        'state_json', 'checkpoint_json', 'decision_log_json',
        'timing_jsonl', 'events_jsonl', 'agent_log',
        'cost_report_json', 'claude_transcript', 'mcp_log'
    )),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE TABLE ingestion_records (
    ingestion_record_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_source_id        uuid NOT NULL
                                    REFERENCES ingestion_sources(ingestion_source_id)
                                    ON DELETE RESTRICT,
    record_key                 text NOT NULL,
    content_sha256             text NOT NULL,
    source_timestamp           timestamptz,
    observed_at                timestamptz NOT NULL DEFAULT now(),
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (ingestion_source_id, record_key),
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX ingestion_records_observed_idx
    ON ingestion_records (ingestion_source_id, observed_at DESC);

-- Record-key contract used by the importer:
--   append-only JSONL:  line:<zero-based-line-number>:<content-sha256>
--   rewritten JSON:     snapshot:<content-sha256>
--   agent log attempt:  attempt:<label>:<attempt-number>:<content-sha256>
-- Never include box/host name in a timing record key.

-- ---------------------------------------------------------------------------
-- 4. Immutable observations of overwritten state.json
-- ---------------------------------------------------------------------------

CREATE TABLE job_state_observations (
    job_state_observation_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL UNIQUE
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    status                     text NOT NULL,
    stage                      text,
    gate                       text,
    question                   text,
    state_redacted             jsonb NOT NULL,
    state_timestamp            timestamptz,
    observed_at                timestamptz NOT NULL,
    CHECK (jsonb_typeof(state_redacted) = 'object')
);

CREATE INDEX job_state_observations_latest_idx
    ON job_state_observations (job_id, observed_at DESC);

-- ---------------------------------------------------------------------------
-- 5. Immutable checkpoint versions
-- ---------------------------------------------------------------------------

CREATE TABLE checkpoints (
    checkpoint_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL UNIQUE
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    stage                      text NOT NULL,
    gate                       text,
    checkpoint_version         text NOT NULL,
    pipeline_type              text NOT NULL,
    status                     text NOT NULL,
    checkpoint_policy          text,
    claimed_human_approval_required boolean NOT NULL DEFAULT false,
    claimed_human_approved     boolean NOT NULL DEFAULT false,
    question                   text,
    artifacts_redacted         jsonb NOT NULL DEFAULT '{}'::jsonb,
    review_redacted            jsonb,
    cost_snapshot_redacted     jsonb,
    metadata_redacted          jsonb,
    partial_progress_redacted  jsonb,
    error_redacted             jsonb,
    checkpoint_timestamp       timestamptz NOT NULL,
    content_sha256             text NOT NULL,
    imported_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, stage, content_sha256),
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (jsonb_typeof(artifacts_redacted) = 'object')
);

CREATE INDEX checkpoints_latest_idx
    ON checkpoints (job_id, stage, checkpoint_timestamp DESC, imported_at DESC);

-- claimed_human_approved is preserved only as the checkpoint's untrusted claim.
-- The approval views below use approval_events, never this column.

-- ---------------------------------------------------------------------------
-- 6. Approval receipts (not proof of a human in Phase 1)
-- ---------------------------------------------------------------------------

CREATE TABLE approval_events (
    approval_event_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    checkpoint_id              uuid REFERENCES checkpoints(checkpoint_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL UNIQUE
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    event_key                  text NOT NULL,
    gate                       text NOT NULL,
    decision                   text NOT NULL,
    answer_redacted            text,
    provenance                 text NOT NULL,
    actor_reference            text,
    received_at                timestamptz NOT NULL,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, event_key),
    CHECK (decision IN ('approve', 'revise', 'skip', 'cancel')),
    CHECK (provenance IN ('legacy_inferred', 'launcher_received', 'dify_signed')),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX approval_events_job_gate_idx
    ON approval_events (job_id, gate, received_at DESC);

-- The Phase 1 writer cannot INSERT approval_events directly. This function fixes
-- the provenance to the only two claims Phase 1 can honestly make. `dify_signed`
-- is reserved for a future, separately privileged verifier.
CREATE OR REPLACE FUNCTION record_phase1_approval(
    p_job_id                  text,
    p_checkpoint_id          uuid,
    p_ingestion_record_id    uuid,
    p_event_key              text,
    p_gate                   text,
    p_decision               text,
    p_answer_redacted        text,
    p_provenance             text,
    p_actor_reference        text,
    p_received_at            timestamptz,
    p_metadata_redacted      jsonb DEFAULT '{}'::jsonb
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = openmontage, pg_temp
AS $$
DECLARE
    v_source_job text;
    v_id uuid;
BEGIN
    IF p_provenance NOT IN ('legacy_inferred', 'launcher_received') THEN
        RAISE EXCEPTION 'Phase 1 cannot write approval provenance %', p_provenance
            USING ERRCODE = '22023';
    END IF;
    IF p_decision NOT IN ('approve', 'revise', 'skip', 'cancel') THEN
        RAISE EXCEPTION 'invalid approval decision %', p_decision
            USING ERRCODE = '22023';
    END IF;

    SELECT s.job_id INTO v_source_job
    FROM ingestion_records r
    JOIN ingestion_sources s USING (ingestion_source_id)
    WHERE r.ingestion_record_id = p_ingestion_record_id;

    IF v_source_job IS DISTINCT FROM p_job_id THEN
        RAISE EXCEPTION 'ingestion record does not belong to job %', p_job_id
            USING ERRCODE = '23514';
    END IF;

    IF p_checkpoint_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM checkpoints c
        WHERE c.checkpoint_id = p_checkpoint_id AND c.job_id = p_job_id
    ) THEN
        RAISE EXCEPTION 'checkpoint does not belong to job %', p_job_id
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO approval_events (
        job_id, checkpoint_id, ingestion_record_id, event_key, gate, decision,
        answer_redacted, provenance, actor_reference, received_at, metadata_redacted
    ) VALUES (
        p_job_id, p_checkpoint_id, p_ingestion_record_id, p_event_key, p_gate,
        p_decision, p_answer_redacted, p_provenance, p_actor_reference,
        p_received_at, coalesce(p_metadata_redacted, '{}'::jsonb)
    )
    ON CONFLICT (job_id, event_key) DO NOTHING
    RETURNING approval_event_id INTO v_id;

    IF v_id IS NULL THEN
        SELECT approval_event_id INTO v_id
        FROM approval_events
        WHERE job_id = p_job_id AND event_key = p_event_key;
    END IF;
    RETURN v_id;
END
$$;

-- ---------------------------------------------------------------------------
-- 7. Decision log entries
-- ---------------------------------------------------------------------------

CREATE TABLE decisions (
    decision_row_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    decision_key               text NOT NULL,
    stage                      text NOT NULL,
    category                   text NOT NULL,
    subject                    text NOT NULL,
    options_considered_redacted jsonb NOT NULL DEFAULT '[]'::jsonb,
    selected_redacted          jsonb NOT NULL,
    reason_redacted            text NOT NULL,
    user_visible               boolean NOT NULL DEFAULT false,
    claimed_user_approved      boolean NOT NULL DEFAULT false,
    confidence                 numeric(5, 4),
    decided_at                 timestamptz,
    imported_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, decision_key),
    CHECK (jsonb_typeof(options_considered_redacted) = 'array'),
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);

CREATE INDEX decisions_job_stage_idx ON decisions (job_id, stage, imported_at);

-- ---------------------------------------------------------------------------
-- 8. Artifact catalogue — metadata only
-- ---------------------------------------------------------------------------

CREATE TABLE artifacts (
    artifact_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    checkpoint_id              uuid REFERENCES checkpoints(checkpoint_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    logical_id                 text NOT NULL,
    artifact_version           integer NOT NULL DEFAULT 1,
    artifact_type              text NOT NULL,
    stage                      text,
    scene_id                   text,
    source_tool                text,
    provider                   text,
    provider_job_id            text,
    model                      text,
    storage_backend            text NOT NULL,
    object_uri                 text NOT NULL,
    mime_type                  text,
    byte_size                  bigint,
    content_sha256             text,
    duration_seconds           numeric(14, 4),
    width_pixels               integer,
    height_pixels              integer,
    fps                        numeric(10, 4),
    artifact_status            text NOT NULL DEFAULT 'available',
    selected                   boolean NOT NULL DEFAULT false,
    rejected_reason_redacted   text,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    expires_at                 timestamptz,
    UNIQUE (job_id, logical_id, artifact_version),
    CHECK (artifact_version > 0),
    CHECK (byte_size IS NULL OR byte_size >= 0),
    CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (object_uri !~* '[?&](x-amz-|signature=|token=|expires=)'),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX artifacts_job_scene_idx ON artifacts (job_id, scene_id, artifact_type);
CREATE INDEX artifacts_provider_job_idx ON artifacts (provider, provider_job_id);
CREATE INDEX artifacts_hash_idx ON artifacts (content_sha256);

-- ---------------------------------------------------------------------------
-- 9. Claude/agent leg attempts — no raw prompt/stdout/stderr
-- ---------------------------------------------------------------------------

CREATE TABLE agent_runs (
    agent_run_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL UNIQUE
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    run_key                    text NOT NULL,
    stage                      text NOT NULL,
    label                      text NOT NULL,
    attempt_no                 integer NOT NULL DEFAULT 1,
    claude_session_id          uuid,
    runner                     text NOT NULL DEFAULT 'claude',
    executable_version         text,
    model                      text,
    effort                     text,
    prompt_sha256              text,
    status                     text NOT NULL,
    exit_code                  integer,
    timeout_seconds            integer,
    started_at                 timestamptz,
    finished_at                timestamptz,
    duration_ms                bigint,
    sanitized_log_object_uri   text,
    sanitized_log_sha256       text,
    error_type                 text,
    error_message_redacted     text,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    imported_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, run_key),
    CHECK (attempt_no > 0),
    CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (sanitized_log_sha256 IS NULL OR sanitized_log_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (sanitized_log_object_uri IS NULL OR
           sanitized_log_object_uri !~* '[?&](x-amz-|signature=|token=|expires=)'),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX agent_runs_job_stage_idx ON agent_runs (job_id, stage, imported_at);
CREATE UNIQUE INDEX agent_runs_claude_session_idx
    ON agent_runs (claude_session_id) WHERE claude_session_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 10. Cost observations from rewritten snapshots or append-only source events
-- ---------------------------------------------------------------------------

CREATE TABLE cost_observations (
    cost_observation_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    agent_run_id               uuid REFERENCES agent_runs(agent_run_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    domain_key                 text NOT NULL,
    provider                   text NOT NULL,
    usage_kind                 text NOT NULL,
    unit                       text NOT NULL,
    amount                     numeric(24, 6) NOT NULL,
    cost_usd                   numeric(18, 6),
    basis                      text NOT NULL,
    estimated                  boolean NOT NULL DEFAULT false,
    observed_at                timestamptz NOT NULL,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (ingestion_record_id, domain_key),
    CHECK (amount >= 0),
    CHECK (cost_usd IS NULL OR cost_usd >= 0),
    CHECK (basis IN ('item', 'aggregate_snapshot')),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX cost_observations_domain_latest_idx
    ON cost_observations (job_id, provider, unit, domain_key, observed_at DESC);

-- Importer contract:
--   Higgsfield item: higgsfield:provider-job:<external-id>
--   Asset fallback:  higgsfield:asset:<asset-id>:revision:<revision-id>
--   ElevenLabs event: elevenlabs:event:<events-line-event-key>
--   Aggregate-only snapshot: <provider>:aggregate:<usage-kind>:<unit>
-- Do not import report-level totals when complete item rows exist for that
-- provider/unit. The totals view below ignores aggregate rows whenever item rows
-- exist, providing a second line of defense.

-- ---------------------------------------------------------------------------
-- 11. Timing events
-- ---------------------------------------------------------------------------

CREATE TABLE timing_events (
    timing_event_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    agent_run_id               uuid REFERENCES agent_runs(agent_run_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL UNIQUE
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    event_key                  text NOT NULL,
    category                   text NOT NULL DEFAULT 'agent_leg',
    stage                      text NOT NULL,
    started_at                 timestamptz,
    finished_at                timestamptz,
    duration_ms                bigint NOT NULL,
    source_timestamp           timestamptz,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, event_key),
    CHECK (duration_ms >= 0),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX timing_events_job_stage_idx ON timing_events (job_id, stage, source_timestamp);

-- timing event_key must be content-derived and box-independent, for example:
-- sha256(job_id || source_timestamp || stage || seconds || normalized_line).

-- ---------------------------------------------------------------------------
-- 12. Transcript archive metadata — never transcript or image bytes
-- ---------------------------------------------------------------------------

CREATE TABLE transcript_archives (
    transcript_archive_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    agent_run_id               uuid REFERENCES agent_runs(agent_run_id) ON DELETE RESTRICT,
    ingestion_record_id        uuid NOT NULL UNIQUE
                                    REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    claude_session_id          uuid NOT NULL,
    archive_kind               text NOT NULL,
    object_uri                 text NOT NULL,
    object_sha256              text NOT NULL,
    byte_size                  bigint NOT NULL,
    embedded_image_count       integer NOT NULL DEFAULT 0,
    sensitive_scan_status      text NOT NULL,
    encryption_key_reference   text,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    expires_at                 timestamptz NOT NULL,
    UNIQUE (claude_session_id, archive_kind, object_sha256),
    CHECK (archive_kind IN ('encrypted_quarantine', 'sanitized_archive')),
    CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (byte_size >= 0),
    CHECK (embedded_image_count >= 0),
    CHECK (sensitive_scan_status IN ('pending', 'clean', 'redacted', 'quarantined')),
    CHECK (object_uri !~* '[?&](x-amz-|signature=|token=|expires=)')
);

CREATE INDEX transcript_archives_expiry_idx ON transcript_archives (expires_at);

-- ---------------------------------------------------------------------------
-- Append-only enforcement
-- ---------------------------------------------------------------------------

CREATE TRIGGER ingestion_sources_immutable
BEFORE UPDATE OR DELETE ON ingestion_sources
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER ingestion_records_immutable
BEFORE UPDATE OR DELETE ON ingestion_records
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER job_state_observations_immutable
BEFORE UPDATE OR DELETE ON job_state_observations
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER checkpoints_immutable
BEFORE UPDATE OR DELETE ON checkpoints
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER approval_events_immutable
BEFORE UPDATE OR DELETE ON approval_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER decisions_immutable
BEFORE UPDATE OR DELETE ON decisions
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER artifacts_immutable
BEFORE UPDATE OR DELETE ON artifacts
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER agent_runs_immutable
BEFORE UPDATE OR DELETE ON agent_runs
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER cost_observations_immutable
BEFORE UPDATE OR DELETE ON cost_observations
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER timing_events_immutable
BEFORE UPDATE OR DELETE ON timing_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

CREATE TRIGGER transcript_archives_immutable
BEFORE UPDATE OR DELETE ON transcript_archives
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

-- ---------------------------------------------------------------------------
-- Read models
-- ---------------------------------------------------------------------------

CREATE VIEW latest_job_states AS
SELECT DISTINCT ON (job_id)
       job_state_observation_id,
       job_id,
       ingestion_record_id,
       status,
       stage,
       gate,
       question,
       state_redacted,
       state_timestamp,
       observed_at
FROM job_state_observations
ORDER BY job_id, observed_at DESC, job_state_observation_id DESC;

CREATE VIEW latest_checkpoints AS
SELECT DISTINCT ON (job_id, stage)
       checkpoint_id,
       job_id,
       stage,
       gate,
       checkpoint_version,
       pipeline_type,
       status,
       checkpoint_policy,
       claimed_human_approval_required,
       claimed_human_approved,
       question,
       artifacts_redacted,
       review_redacted,
       cost_snapshot_redacted,
       metadata_redacted,
       partial_progress_redacted,
       error_redacted,
       checkpoint_timestamp,
       content_sha256,
       imported_at
FROM checkpoints
ORDER BY job_id, stage, checkpoint_timestamp DESC, imported_at DESC, checkpoint_id DESC;

CREATE VIEW checkpoint_approval_status AS
SELECT
    c.*,
    coalesce(a.approval_received, false) AS approval_received,
    a.decision AS received_decision,
    a.provenance AS approval_provenance,
    a.received_at AS approval_received_at
FROM latest_checkpoints c
LEFT JOIN LATERAL (
    SELECT
        (ae.decision = 'approve') AS approval_received,
        ae.decision,
        ae.provenance,
        ae.received_at
    FROM approval_events ae
    WHERE ae.job_id = c.job_id
      AND (ae.checkpoint_id = c.checkpoint_id OR
           (ae.checkpoint_id IS NULL AND ae.gate = c.gate))
    ORDER BY ae.received_at DESC, ae.approval_event_id DESC
    LIMIT 1
) a ON true;

CREATE VIEW current_cost_observations AS
SELECT DISTINCT ON (job_id, provider, unit, domain_key)
       cost_observation_id,
       job_id,
       agent_run_id,
       ingestion_record_id,
       domain_key,
       provider,
       usage_kind,
       unit,
       amount,
       cost_usd,
       basis,
       estimated,
       observed_at,
       metadata_redacted
FROM cost_observations
ORDER BY job_id, provider, unit, domain_key, observed_at DESC, cost_observation_id DESC;

CREATE VIEW effective_cost_observations AS
WITH current_rows AS (
    SELECT * FROM current_cost_observations
), item_presence AS (
    SELECT job_id, provider, unit, bool_or(basis = 'item') AS has_items
    FROM current_rows
    GROUP BY job_id, provider, unit
)
SELECT c.*
FROM current_rows c
JOIN item_presence p USING (job_id, provider, unit)
WHERE c.basis = 'item'
   OR (c.basis = 'aggregate_snapshot' AND NOT p.has_items);

CREATE VIEW job_cost_totals AS
SELECT
    job_id,
    provider,
    unit,
    sum(amount) AS amount,
    sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS cost_usd,
    bool_or(estimated) AS includes_estimates,
    count(*) AS contributing_rows
FROM effective_cost_observations
GROUP BY job_id, provider, unit;

CREATE VIEW job_timing_totals AS
SELECT
    job_id,
    stage,
    sum(duration_ms) AS duration_ms,
    count(*) AS event_count
FROM timing_events
GROUP BY job_id, stage;

CREATE OR REPLACE FUNCTION get_job_resume_context(p_job_id text)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = openmontage, pg_temp
AS $$
SELECT jsonb_build_object(
    'job', jsonb_build_object(
        'job_id', j.job_id,
        'pipeline', j.pipeline,
        'profile', j.profile,
        'brief', j.brief,
        'options', j.options_redacted,
        'status', j.current_status,
        'stage', j.current_stage,
        'gate', j.current_gate,
        'question', j.current_question,
        'state_version', j.state_version,
        'updated_at', j.updated_at
    ),
    'latest_state', coalesce((
        SELECT to_jsonb(s) - 'job_state_observation_id' - 'ingestion_record_id'
        FROM latest_job_states s
        WHERE s.job_id = j.job_id
    ), '{}'::jsonb),
    'checkpoints', coalesce((
        SELECT jsonb_agg(
            to_jsonb(c) - 'ingestion_record_id'
            ORDER BY c.checkpoint_timestamp
        )
        FROM checkpoint_approval_status c
        WHERE c.job_id = j.job_id
    ), '[]'::jsonb),
    'decisions', coalesce((
        SELECT jsonb_agg(
            jsonb_build_object(
                'decision_key', d.decision_key,
                'stage', d.stage,
                'category', d.category,
                'subject', d.subject,
                'selected', d.selected_redacted,
                'reason', d.reason_redacted,
                'confidence', d.confidence,
                'decided_at', d.decided_at
            ) ORDER BY d.imported_at
        )
        FROM decisions d
        WHERE d.job_id = j.job_id
    ), '[]'::jsonb),
    'artifacts', coalesce((
        SELECT jsonb_agg(
            jsonb_build_object(
                'logical_id', a.logical_id,
                'version', a.artifact_version,
                'type', a.artifact_type,
                'stage', a.stage,
                'scene_id', a.scene_id,
                'provider', a.provider,
                'model', a.model,
                'object_uri', a.object_uri,
                'sha256', a.content_sha256,
                'status', a.artifact_status,
                'selected', a.selected,
                'metadata', a.metadata_redacted
            ) ORDER BY a.created_at
        )
        FROM artifacts a
        WHERE a.job_id = j.job_id
    ), '[]'::jsonb),
    'agent_runs', coalesce((
        SELECT jsonb_agg(
            jsonb_build_object(
                'run_key', r.run_key,
                'stage', r.stage,
                'label', r.label,
                'attempt', r.attempt_no,
                'session_id', r.claude_session_id,
                'status', r.status,
                'duration_ms', r.duration_ms,
                'error_type', r.error_type
            ) ORDER BY r.imported_at
        )
        FROM agent_runs r
        WHERE r.job_id = j.job_id
    ), '[]'::jsonb),
    'costs', coalesce((
        SELECT jsonb_agg(to_jsonb(c) - 'job_id' ORDER BY c.provider, c.unit)
        FROM job_cost_totals c
        WHERE c.job_id = j.job_id
    ), '[]'::jsonb),
    'timing', coalesce((
        SELECT jsonb_agg(to_jsonb(t) - 'job_id' ORDER BY t.stage)
        FROM job_timing_totals t
        WHERE t.job_id = j.job_id
    ), '[]'::jsonb)
)
FROM jobs j
WHERE j.job_id = p_job_id
$$;

-- ---------------------------------------------------------------------------
-- Runtime privileges
-- ---------------------------------------------------------------------------

REVOKE ALL ON SCHEMA openmontage FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA openmontage FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA openmontage FROM PUBLIC;

GRANT USAGE ON SCHEMA openmontage TO om_phase1_writer, om_phase1_reader;

GRANT SELECT, INSERT ON jobs TO om_phase1_writer;
GRANT UPDATE (
    current_status, current_stage, current_gate, current_question, state_version,
    code_branch, code_commit, runner_version, processing_started_at,
    processing_finished_at, completed_at, failed_at, terminal_error_redacted
) ON jobs TO om_phase1_writer;

GRANT SELECT, INSERT ON
    ingestion_sources,
    ingestion_records,
    job_state_observations,
    checkpoints,
    decisions,
    artifacts,
    agent_runs,
    cost_observations,
    timing_events,
    transcript_archives
TO om_phase1_writer;

GRANT SELECT ON approval_events TO om_phase1_writer;
GRANT EXECUTE ON FUNCTION record_phase1_approval(
    text, uuid, uuid, text, text, text, text, text, text, timestamptz, jsonb
) TO om_phase1_writer;

GRANT SELECT ON
    latest_job_states,
    latest_checkpoints,
    checkpoint_approval_status,
    current_cost_observations,
    effective_cost_observations,
    job_cost_totals,
    job_timing_totals
TO om_phase1_writer;

GRANT SELECT ON ALL TABLES IN SCHEMA openmontage TO om_phase1_reader;
GRANT EXECUTE ON FUNCTION get_job_resume_context(text) TO om_phase1_writer, om_phase1_reader;

-- Runtime roles cannot make future tables writable automatically. Every later
-- migration must grant only the explicit minimum privileges it needs.

COMMIT;

-- Deployment requirements outside this SQL file:
--
-- 1. Create a LOGIN role separately and GRANT om_phase1_writer to it. Keep its
--    credential readable only by a dedicated ingestion OS user (mode 0600).
-- 2. Do not put DATABASE_URL in the launcher .env. Claude inherits that process env.
-- 3. Keep files as source of truth during dual-write/backfill and reconcile counts,
--    hashes, costs, timings, checkpoints, and current state before any cutover.
-- 4. Use --session-id with a preallocated UUID for new Claude legs so agent_runs
--    can map a session to a job/stage without timestamp inference.
-- 5. Set a finite object-store lifecycle for encrypted transcript quarantine.
--    Long-lived sanitized archives must contain no raw images or credentials.
-- 6. Phase 2 should add Dify/user/conversation identity and signed, nonce-protected
--    approval events. Until then, no approval is labelled verified_human.
