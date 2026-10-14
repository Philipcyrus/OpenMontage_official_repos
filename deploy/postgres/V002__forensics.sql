-- OpenMontage / Panda AI — V002 forensic trail (applies on top of V001__phase1.sql)
-- PostgreSQL 14+
--
-- Adds the interaction layer V001 deliberately left out, built by deploy/panda_ingest.py from
-- files that already exist on the box (job folder + Claude transcripts + MCP logs):
--   * llm_inferences  one row per Claude model response (streamed copies deduplicated)
--   * llm_blocks      every visible prompt/assistant/tool block, sanitized
--   * trace_spans     timed rows: legs, attempts, inferences, tool calls, provider jobs,
--                     local tool runs and the gaps between legs
--   * span_links      non-tree relations (a batch creates N provider jobs, a wait polls N,
--                     a provider job produces an artifact)
--   * runtime_events  typed facts that are not durations (provider outcomes, API retries,
--                     connector status, QA results, approvals received, crashes)
-- and fixes V001 defects found when testing it against the real file formats:
--   1. artifacts: selection/status could never change (append-only) -> lifecycle columns are
--      updatable, identity columns stay immutable, facts learned later (provider, model from
--      the manifest) can be filled once; uniqueness is by content, not by an importer-assigned
--      version number.
--   2. object_uri CHECKs missed GCS/Azure/Dify signatures -> no query string or fragment at all
--      (the exporter encodes '?' and '#' in local file names).
--   3. TRUNCATE bypassed the append-only triggers -> statement-level guards.
--   4. Nothing could ever be erased (retention, a user's erasure request) -> owner-only
--      purge_job() through a transaction-local switch the triggers honour.
--   5. latest_job_states ordered by import time -> orders by the state's own timestamp.
--   6. checkpoint_approval_status matched an approval only to the exact checkpoint row, so an
--      approval of the awaiting version never showed on the completed version it produced,
--      and attached old approvals to later re-runs -> a decision counts for the stage's
--      latest version when it was received after the previous version and by the time this
--      one was written.
--   7. current_cost_observations kept items that vanished from later manifest snapshots
--      (double counting after a retry) -> a snapshot source counts only its latest snapshot
--      (by the source's records, so an emptied manifest counts as empty); job_cost_totals is
--      split by usage_kind (manifest credits vs quoted submissions). Both cost views filter
--      by job before they aggregate.
--   8. ingestion_sources.source_kind rejected real sources (project files, artifacts).
--   9. The writer could not update options/inputs although they change mid-job.
--  10. Foreign-key columns without an index made every purge scan whole tables.
-- Not included, deliberately: encrypted copies of original payloads (Codex's raw_payloads).
-- Originals stay in the access-restricted files on the box for their retention period;
-- only sanitized content is stored here.

BEGIN;

SET search_path TO openmontage, public;

-- ---------------------------------------------------------------------------
-- Append-only guard with an owner-only erasure switch (replaces V001's function)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE')
       AND coalesce(current_setting('openmontage.purge', true), '') = 'on' THEN
        IF TG_LEVEL = 'ROW' THEN
            RETURN OLD;
        END IF;
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'append-only audit table % cannot be updated, deleted or truncated',
        TG_TABLE_NAME USING ERRCODE = '55000';
END
$$;

-- ---------------------------------------------------------------------------
-- Extensions to V001 tables
-- ---------------------------------------------------------------------------

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS trace_id uuid;

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS root_span_id uuid,
    ADD COLUMN IF NOT EXISTS input_tokens bigint,
    ADD COLUMN IF NOT EXISTS output_tokens bigint,
    ADD COLUMN IF NOT EXISTS cache_creation_input_tokens bigint,
    ADD COLUMN IF NOT EXISTS cache_read_input_tokens bigint,
    ADD COLUMN IF NOT EXISTS thinking_tokens bigint,
    ADD COLUMN IF NOT EXISTS inference_count integer,
    ADD COLUMN IF NOT EXISTS tool_call_count integer,
    ADD COLUMN IF NOT EXISTS tool_error_count integer,
    ADD COLUMN IF NOT EXISTS session_mapping text,
    ADD COLUMN IF NOT EXISTS session_mapping_confidence numeric(4, 3);

COMMENT ON COLUMN agent_runs.thinking_tokens IS
    'Reported separately by Claude Code but already included in output_tokens; never add them.';

ALTER TABLE timing_events ADD COLUMN IF NOT EXISTS span_id uuid;
ALTER TABLE cost_observations ADD COLUMN IF NOT EXISTS span_id uuid;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS producing_span_id uuid;

ALTER TABLE ingestion_sources DROP CONSTRAINT IF EXISTS ingestion_sources_source_kind_check;
ALTER TABLE ingestion_sources ADD CONSTRAINT ingestion_sources_source_kind_check CHECK (
    source_kind IN (
        'state_json', 'checkpoint_json', 'decision_log_json',
        'timing_jsonl', 'events_jsonl', 'agent_log',
        'cost_report_json', 'claude_transcript', 'mcp_log',
        'project_json', 'project_file', 'artifact_file', 'qa_report', 'audio_report'
    )
);

-- (2) No query strings or fragments in stored locations: signed links never belong here.
ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_object_uri_check;
ALTER TABLE artifacts ADD CONSTRAINT artifacts_object_uri_check CHECK (object_uri !~ '[?#]');
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_sanitized_log_object_uri_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_sanitized_log_object_uri_check
    CHECK (sanitized_log_object_uri IS NULL OR sanitized_log_object_uri !~ '[?#]');
ALTER TABLE transcript_archives DROP CONSTRAINT IF EXISTS transcript_archives_object_uri_check;
ALTER TABLE transcript_archives ADD CONSTRAINT transcript_archives_object_uri_check
    CHECK (object_uri !~ '[?#]');

-- (1) Artifacts: identity is (job, logical path, content); lifecycle columns may change.
ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_job_id_logical_id_artifact_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS artifacts_content_key
    ON artifacts (job_id, logical_id, content_sha256);

CREATE OR REPLACE FUNCTION guard_artifact_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF coalesce(current_setting('openmontage.purge', true), '') = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'artifacts rows cannot be deleted' USING ERRCODE = '55000';
    END IF;
    -- Identity: which file, which content, where. Never changes.
    IF (NEW.artifact_id, NEW.job_id, NEW.checkpoint_id, NEW.ingestion_record_id,
        NEW.logical_id, NEW.artifact_version, NEW.artifact_type, NEW.storage_backend,
        NEW.object_uri, NEW.byte_size, NEW.content_sha256, NEW.created_at)
       IS DISTINCT FROM
       (OLD.artifact_id, OLD.job_id, OLD.checkpoint_id, OLD.ingestion_record_id,
        OLD.logical_id, OLD.artifact_version, OLD.artifact_type, OLD.storage_backend,
        OLD.object_uri, OLD.byte_size, OLD.content_sha256, OLD.created_at) THEN
        RAISE EXCEPTION 'an artifact''s identity (file, content, location) cannot change'
            USING ERRCODE = '55000';
    END IF;
    -- Facts often learned after the file appeared (the manifest lists a clip at the end of a
    -- leg): each can be filled once, never rewritten.
    IF (OLD.stage IS NOT NULL AND NEW.stage IS DISTINCT FROM OLD.stage)
       OR (OLD.scene_id IS NOT NULL AND NEW.scene_id IS DISTINCT FROM OLD.scene_id)
       OR (OLD.source_tool IS NOT NULL AND NEW.source_tool IS DISTINCT FROM OLD.source_tool)
       OR (OLD.provider IS NOT NULL AND NEW.provider IS DISTINCT FROM OLD.provider)
       OR (OLD.provider_job_id IS NOT NULL
           AND NEW.provider_job_id IS DISTINCT FROM OLD.provider_job_id)
       OR (OLD.model IS NOT NULL AND NEW.model IS DISTINCT FROM OLD.model)
       OR (OLD.mime_type IS NOT NULL AND NEW.mime_type IS DISTINCT FROM OLD.mime_type)
       OR (OLD.duration_seconds IS NOT NULL
           AND NEW.duration_seconds IS DISTINCT FROM OLD.duration_seconds)
       OR (OLD.width_pixels IS NOT NULL AND NEW.width_pixels IS DISTINCT FROM OLD.width_pixels)
       OR (OLD.height_pixels IS NOT NULL
           AND NEW.height_pixels IS DISTINCT FROM OLD.height_pixels)
       OR (OLD.fps IS NOT NULL AND NEW.fps IS DISTINCT FROM OLD.fps)
       OR (OLD.producing_span_id IS NOT NULL
           AND NEW.producing_span_id IS DISTINCT FROM OLD.producing_span_id) THEN
        RAISE EXCEPTION 'artifact facts can be filled in once but not changed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS artifacts_immutable ON artifacts;
CREATE TRIGGER artifacts_lifecycle_only
BEFORE UPDATE OR DELETE ON artifacts
FOR EACH ROW EXECUTE FUNCTION guard_artifact_update();

-- ---------------------------------------------------------------------------
-- 13. trace_spans
-- ---------------------------------------------------------------------------

CREATE TABLE trace_spans (
    span_id                    uuid PRIMARY KEY,
    trace_id                   uuid NOT NULL,
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    agent_run_id               uuid,
    claude_session_id          uuid,
    parent_span_id             uuid,
    operation_id               uuid,
    ingestion_record_id        uuid REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    span_kind                  text NOT NULL,
    name                       text NOT NULL,
    source_service             text NOT NULL,
    stage                      text,
    scene_id                   text,
    attempt_no                 integer,
    tool_use_id                text,
    provider_job_id_hash       text,
    transport_status           text NOT NULL,
    semantic_outcome           text,
    started_at                 timestamptz,
    ended_at                   timestamptz,
    duration_ms                bigint,
    error_class                text,
    error_message_redacted     text,
    request_block_id           uuid,
    response_block_id          uuid,
    attributes_redacted        jsonb NOT NULL DEFAULT '{}'::jsonb,
    correlation_method         text NOT NULL,
    correlation_confidence     numeric(4, 3) NOT NULL DEFAULT 1,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    CHECK (span_kind IN (
        'job', 'agent_leg', 'claude_session', 'llm_inference', 'tool_call',
        'provider_job', 'gap', 'render', 'qa'
    )),
    CHECK (transport_status IN ('started', 'success', 'error', 'timeout', 'cancelled', 'unknown')),
    CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CHECK (attempt_no IS NULL OR attempt_no >= 0),
    CHECK (correlation_confidence >= 0 AND correlation_confidence <= 1),
    CHECK (provider_job_id_hash IS NULL OR provider_job_id_hash ~ '^[0-9a-f]{16,64}$'),
    CHECK (jsonb_typeof(attributes_redacted) = 'object')
);

CREATE INDEX trace_spans_job_time_idx ON trace_spans (job_id, started_at);
CREATE INDEX trace_spans_kind_idx ON trace_spans (job_id, span_kind, name);
CREATE INDEX trace_spans_parent_idx ON trace_spans (parent_span_id);
CREATE INDEX trace_spans_session_idx ON trace_spans (claude_session_id);
CREATE INDEX trace_spans_tool_use_idx ON trace_spans (tool_use_id) WHERE tool_use_id IS NOT NULL;
CREATE INDEX trace_spans_provider_job_idx
    ON trace_spans (provider_job_id_hash) WHERE provider_job_id_hash IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 14. span_links
-- ---------------------------------------------------------------------------

CREATE TABLE span_links (
    span_link_id               uuid PRIMARY KEY,
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    from_span_id               uuid NOT NULL,
    to_span_id                 uuid,
    to_artifact_id             uuid,
    relation                   text NOT NULL,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    correlation_method         text NOT NULL,
    correlation_confidence     numeric(4, 3) NOT NULL DEFAULT 1,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    CHECK (to_span_id IS NOT NULL OR to_artifact_id IS NOT NULL),
    CHECK (relation IN (
        'creates', 'polls', 'returns', 'retry_of', 'produces', 'downloaded_as',
        'rejected_by', 'selected_in', 'replaced_by', 'uses_attachment', 'approved_by',
        'follows'
    )),
    CHECK (correlation_confidence >= 0 AND correlation_confidence <= 1),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX span_links_from_idx ON span_links (from_span_id, relation);
CREATE INDEX span_links_to_idx ON span_links (to_span_id, relation);
CREATE INDEX span_links_artifact_idx ON span_links (to_artifact_id) WHERE to_artifact_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 15. llm_inferences
-- ---------------------------------------------------------------------------

CREATE TABLE llm_inferences (
    llm_inference_id           uuid PRIMARY KEY,
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    agent_run_id               uuid,
    span_id                    uuid,
    claude_session_id          uuid NOT NULL,
    model_message_id           text NOT NULL,
    request_id                 text,
    first_sequence_no          bigint NOT NULL,
    last_sequence_no           bigint NOT NULL,
    model                      text,
    service_tier               text,
    stop_reason                text,
    input_tokens               bigint,
    cache_creation_input_tokens bigint,
    cache_read_input_tokens    bigint,
    output_tokens              bigint,
    thinking_tokens            bigint,
    block_count                integer NOT NULL DEFAULT 0,
    tool_use_count             integer NOT NULL DEFAULT 0,
    started_at                 timestamptz,
    finished_at                timestamptz,
    duration_ms                bigint,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    UNIQUE (claude_session_id, model_message_id),
    CHECK (last_sequence_no >= first_sequence_no),
    CHECK (duration_ms IS NULL OR duration_ms >= 0),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

COMMENT ON COLUMN llm_inferences.thinking_tokens IS
    'Already included in output_tokens; never add them.';

CREATE INDEX llm_inferences_job_idx ON llm_inferences (job_id, started_at);
CREATE INDEX llm_inferences_run_idx ON llm_inferences (agent_run_id);

-- ---------------------------------------------------------------------------
-- 16. llm_blocks
-- ---------------------------------------------------------------------------

CREATE TABLE llm_blocks (
    llm_block_id               uuid PRIMARY KEY,
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    llm_inference_id           uuid,
    agent_run_id               uuid,
    span_id                    uuid,
    claude_session_id          uuid NOT NULL,
    source_uuid                text NOT NULL,
    parent_source_uuid         text,
    message_id                 text,
    sequence_no                bigint NOT NULL,
    block_index                integer NOT NULL,
    role                       text,
    block_type                 text NOT NULL,
    tool_use_id                text,
    tool_name                  text,
    is_error                   boolean,
    is_sidechain               boolean NOT NULL DEFAULT false,
    content_text_redacted      text,
    content_json_redacted      jsonb,
    content_sha256             text,
    content_bytes              bigint,
    content_truncated          boolean NOT NULL DEFAULT false,
    content_omitted_reason     text,
    occurred_at                timestamptz,
    metadata_redacted          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    UNIQUE (claude_session_id, source_uuid, block_index),
    CHECK (block_index >= 0),
    CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (content_bytes IS NULL OR content_bytes >= 0),
    CHECK (jsonb_typeof(metadata_redacted) = 'object')
);

CREATE INDEX llm_blocks_session_seq_idx ON llm_blocks (claude_session_id, sequence_no, block_index);
CREATE INDEX llm_blocks_job_time_idx ON llm_blocks (job_id, occurred_at);
CREATE INDEX llm_blocks_tool_use_idx ON llm_blocks (tool_use_id) WHERE tool_use_id IS NOT NULL;
CREATE INDEX llm_blocks_tool_name_idx ON llm_blocks (job_id, tool_name) WHERE tool_name IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 17. runtime_events
-- ---------------------------------------------------------------------------

CREATE TABLE runtime_events (
    runtime_event_id           uuid PRIMARY KEY,
    job_id                     text NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    trace_id                   uuid,
    span_id                    uuid,
    agent_run_id               uuid,
    ingestion_record_id        uuid REFERENCES ingestion_records(ingestion_record_id)
                                    ON DELETE RESTRICT,
    dedupe_key                 text NOT NULL,
    event_name                 text NOT NULL,
    source_service             text NOT NULL,
    severity                   text NOT NULL,
    stage                      text,
    scene_id                   text,
    artifact_id                uuid,
    outcome                    text,
    occurred_at                timestamptz,
    message_redacted           text,
    attributes_redacted        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, dedupe_key),
    CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    CHECK (jsonb_typeof(attributes_redacted) = 'object')
);

CREATE INDEX runtime_events_job_time_idx ON runtime_events (job_id, occurred_at);
CREATE INDEX runtime_events_name_idx ON runtime_events (event_name, occurred_at);

-- ---------------------------------------------------------------------------
-- (10) Indexes on referencing columns: ON DELETE RESTRICT checks (purge_job) and the
-- per-job views would otherwise scan whole tables.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS trace_spans_record_idx
    ON trace_spans (ingestion_record_id) WHERE ingestion_record_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS runtime_events_record_idx
    ON runtime_events (ingestion_record_id) WHERE ingestion_record_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS span_links_job_idx ON span_links (job_id);
CREATE INDEX IF NOT EXISTS artifacts_record_idx ON artifacts (ingestion_record_id);
CREATE INDEX IF NOT EXISTS artifacts_checkpoint_idx
    ON artifacts (checkpoint_id) WHERE checkpoint_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS artifacts_logical_idx ON artifacts (job_id, logical_id, created_at);
CREATE INDEX IF NOT EXISTS decisions_record_idx ON decisions (ingestion_record_id);
CREATE INDEX IF NOT EXISTS cost_observations_run_idx
    ON cost_observations (agent_run_id) WHERE agent_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS timing_events_run_idx
    ON timing_events (agent_run_id) WHERE agent_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS transcript_archives_run_idx
    ON transcript_archives (agent_run_id) WHERE agent_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS transcript_archives_job_idx ON transcript_archives (job_id);
CREATE INDEX IF NOT EXISTS approval_events_checkpoint_idx
    ON approval_events (checkpoint_id) WHERE checkpoint_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ingestion_records_latest_idx
    ON ingestion_records (ingestion_source_id, source_timestamp DESC NULLS LAST,
                          observed_at DESC);

-- ---------------------------------------------------------------------------
-- Append-only enforcement: row triggers on the new tables, TRUNCATE guards on all (3)
-- ---------------------------------------------------------------------------

CREATE TRIGGER trace_spans_immutable BEFORE UPDATE OR DELETE ON trace_spans
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
CREATE TRIGGER span_links_immutable BEFORE UPDATE OR DELETE ON span_links
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
CREATE TRIGGER llm_inferences_immutable BEFORE UPDATE OR DELETE ON llm_inferences
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
CREATE TRIGGER llm_blocks_immutable BEFORE UPDATE OR DELETE ON llm_blocks
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
CREATE TRIGGER runtime_events_immutable BEFORE UPDATE OR DELETE ON runtime_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'jobs', 'ingestion_sources', 'ingestion_records', 'job_state_observations',
        'checkpoints', 'approval_events', 'decisions', 'artifacts', 'agent_runs',
        'cost_observations', 'timing_events', 'transcript_archives', 'trace_spans',
        'span_links', 'llm_inferences', 'llm_blocks', 'runtime_events'
    ] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_mutation()',
                       t || '_no_truncate', t);
    END LOOP;
END
$$;

-- jobs is a projection (updates allowed) but rows must not vanish outside a purge.
CREATE OR REPLACE FUNCTION guard_job_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF coalesce(current_setting('openmontage.purge', true), '') = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'jobs rows can only be removed with purge_job()' USING ERRCODE = '55000';
END
$$;

CREATE TRIGGER jobs_no_delete BEFORE DELETE ON jobs
FOR EACH ROW EXECUTE FUNCTION guard_job_delete();

-- ---------------------------------------------------------------------------
-- (4) Owner-only erasure: every row of one job, in dependency order. Database rows only:
-- deploy/README.md lists the files on the box (spool, exporter state, job folders) that an
-- erasure must also remove, or the exporter sends the job again.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION purge_job(p_job_id text)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = openmontage, pg_temp
AS $$
DECLARE
    v_rows integer := 0;
    v_count integer;
BEGIN
    PERFORM set_config('openmontage.purge', 'on', true);
    DELETE FROM runtime_events WHERE job_id = p_job_id;     GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM span_links WHERE job_id = p_job_id;         GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM trace_spans WHERE job_id = p_job_id;        GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM llm_blocks WHERE job_id = p_job_id;         GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM llm_inferences WHERE job_id = p_job_id;     GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM approval_events WHERE job_id = p_job_id;    GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM transcript_archives WHERE job_id = p_job_id; GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM timing_events WHERE job_id = p_job_id;      GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM cost_observations WHERE job_id = p_job_id;  GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM artifacts WHERE job_id = p_job_id;          GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM agent_runs WHERE job_id = p_job_id;         GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM decisions WHERE job_id = p_job_id;          GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM checkpoints WHERE job_id = p_job_id;        GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM job_state_observations WHERE job_id = p_job_id; GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM ingestion_records r USING ingestion_sources s
        WHERE r.ingestion_source_id = s.ingestion_source_id AND s.job_id = p_job_id;
    GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM ingestion_sources WHERE job_id = p_job_id;  GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    DELETE FROM jobs WHERE job_id = p_job_id;               GET DIAGNOSTICS v_count = ROW_COUNT; v_rows := v_rows + v_count;
    PERFORM set_config('openmontage.purge', 'off', true);
    RETURN v_rows;
END
$$;

REVOKE ALL ON FUNCTION purge_job(text) FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- (5)-(7) Read models fixed
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW latest_job_states AS
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
ORDER BY job_id, state_timestamp DESC NULLS LAST, observed_at DESC, job_state_observation_id DESC;

-- The decision behind each stage's latest checkpoint. An approval names the version the
-- human saw (awaiting_human) while the latest version is the one written after it
-- (completed), and a revise names only the stage. So a decision counts for the latest
-- version when it is about the same stage (by its checkpoint, by 'stage:<stage>' or by the
-- gate) and was received after the previous version and by the time this one was written.
CREATE OR REPLACE VIEW checkpoint_approval_status AS
SELECT
    c.*,
    coalesce(a.approval_received, false) AS approval_received,
    a.decision AS received_decision,
    a.provenance AS approval_provenance,
    a.received_at AS approval_received_at
FROM latest_checkpoints c
LEFT JOIN LATERAL (
    SELECT max(p.checkpoint_timestamp) AS previous_at
    FROM checkpoints p
    WHERE p.job_id = c.job_id AND p.stage = c.stage
      AND p.checkpoint_timestamp < c.checkpoint_timestamp
) prev ON true
LEFT JOIN LATERAL (
    SELECT
        (ae.decision = 'approve') AS approval_received,
        ae.decision,
        ae.provenance,
        ae.received_at
    FROM approval_events ae
    LEFT JOIN checkpoints ac ON ac.checkpoint_id = ae.checkpoint_id
    WHERE ae.job_id = c.job_id
      AND (ae.checkpoint_id = c.checkpoint_id
           OR ((ac.stage = c.stage OR ae.gate = 'stage:' || c.stage
                OR (c.gate IS NOT NULL AND ae.gate = c.gate))
               AND (prev.previous_at IS NULL OR ae.received_at > prev.previous_at)
               AND ae.received_at <= c.checkpoint_timestamp + interval '5 seconds'))
    ORDER BY ae.received_at DESC, ae.approval_event_id DESC
    LIMIT 1
) a ON true;

-- A snapshot row (manifest credits, an aggregate) counts only when its record is its
-- source's latest record: an item dropped from a later manifest stops counting, and so does
-- everything when the latest manifest has no credited item. The exporter keeps one source
-- per job for the asset manifest, whichever copy of it was read. Written without a shared
-- CTE so that a job_id filter reaches the base tables.
CREATE OR REPLACE VIEW current_cost_observations AS
SELECT DISTINCT ON (c.job_id, c.provider, c.unit, c.domain_key)
       c.cost_observation_id,
       c.job_id,
       c.agent_run_id,
       c.ingestion_record_id,
       c.domain_key,
       c.provider,
       c.usage_kind,
       c.unit,
       c.amount,
       c.cost_usd,
       c.basis,
       c.estimated,
       c.observed_at,
       c.metadata_redacted
FROM cost_observations c
JOIN ingestion_records r ON r.ingestion_record_id = c.ingestion_record_id
WHERE NOT (c.usage_kind = 'manifest_credits' OR c.basis = 'aggregate_snapshot')
   OR c.ingestion_record_id = (
        SELECT l.ingestion_record_id
        FROM ingestion_records l
        WHERE l.ingestion_source_id = r.ingestion_source_id
        ORDER BY l.source_timestamp DESC NULLS LAST, l.observed_at DESC,
                 l.ingestion_record_id DESC
        LIMIT 1)
ORDER BY c.job_id, c.provider, c.unit, c.domain_key, c.observed_at DESC, c.cost_observation_id DESC;

-- V001's version used a CTE referenced twice (materialized: every job's rows were sorted
-- for a single-job query). A window partitioned by job keeps the job filter pushed down.
CREATE OR REPLACE VIEW effective_cost_observations AS
SELECT cost_observation_id, job_id, agent_run_id, ingestion_record_id, domain_key, provider,
       usage_kind, unit, amount, cost_usd, basis, estimated, observed_at, metadata_redacted
FROM (
    SELECT c.*,
           bool_or(c.basis = 'item') OVER (PARTITION BY c.job_id, c.provider, c.unit)
               AS has_items
    FROM current_cost_observations c
) x
WHERE x.basis = 'item'
   OR (x.basis = 'aggregate_snapshot' AND NOT x.has_items);

CREATE OR REPLACE VIEW job_cost_totals AS
SELECT
    job_id,
    provider,
    unit,
    sum(amount) AS amount,
    sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL) AS cost_usd,
    bool_or(estimated) AS includes_estimates,
    count(*) AS contributing_rows,
    usage_kind
FROM effective_cost_observations
GROUP BY job_id, provider, unit, usage_kind;

-- ---------------------------------------------------------------------------
-- Operator views over the forensic tables
-- ---------------------------------------------------------------------------

-- Every tool call with its sanitized arguments and result, in order. A call made while its
-- leg was still running has no stage on its span; the leg's agent run carries it.
CREATE VIEW tool_calls_v AS
SELECT s.job_id,
       coalesce(s.stage, ar.stage) AS stage,
       s.agent_run_id,
       s.claude_session_id,
       s.started_at,
       s.ended_at,
       s.duration_ms,
       s.name AS tool_name,
       s.tool_use_id,
       s.transport_status,
       s.semantic_outcome,
       s.error_message_redacted,
       req.content_json_redacted AS arguments_redacted,
       coalesce(res.content_json_redacted, to_jsonb(res.content_text_redacted)) AS result_redacted,
       res.content_truncated AS result_truncated,
       s.attributes_redacted
FROM trace_spans s
LEFT JOIN agent_runs ar ON ar.agent_run_id = s.agent_run_id
LEFT JOIN llm_blocks req ON req.llm_block_id = s.request_block_id
LEFT JOIN llm_blocks res ON res.llm_block_id = s.response_block_id
WHERE s.span_kind = 'tool_call' AND s.source_service <> 'openmontage_tool';

-- Calls that failed in transport, or returned without doing the work that was asked.
CREATE VIEW failed_interactions_v AS
SELECT *
FROM tool_calls_v
WHERE transport_status IN ('error', 'timeout', 'cancelled')
   OR semantic_outcome IN ('error', 'malformed_arguments', 'rejected_credits', 'unauthorized',
                           'failed', 'recommendation_only', 'no_job_returned',
                           'terminal_with_failures', 'media_not_confirmed', 'timeout',
                           'interrupted', 'partially_submitted');

-- Each provider job: how it was created and every poll, with this job's own status in each
-- call (a wave poll covers several jobs) and the call's overall outcome. Identity comes from
-- the link, so jobs still in flight (no provider span yet) are told apart too.
CREATE VIEW provider_job_history_v AS
SELECT l.job_id,
       l.to_span_id AS provider_span_id,
       coalesce(p.provider_job_id_hash, l.metadata_redacted ->> 'provider_job_id_hash',
                t.provider_job_id_hash) AS provider_job_id_hash,
       p.started_at AS provider_first_seen_at,
       p.ended_at AS provider_terminal_seen_at,
       p.semantic_outcome AS provider_outcome,
       l.relation,
       l.metadata_redacted ->> 'job_outcome' AS job_outcome_in_call,
       l.metadata_redacted ->> 'job_status' AS job_status_in_call,
       t.name AS tool_name,
       t.started_at AS call_started_at,
       t.ended_at AS call_ended_at,
       t.semantic_outcome AS call_outcome,
       t.attributes_redacted AS call_attributes
FROM span_links l
JOIN trace_spans t ON t.span_id = l.from_span_id
LEFT JOIN trace_spans p ON p.span_id = l.to_span_id
WHERE l.relation IN ('creates', 'polls');

-- Files each leg read, and how often (context waste); content is never stored.
CREATE VIEW context_reads_v AS
SELECT b.job_id,
       b.agent_run_id,
       b.claude_session_id,
       b.content_json_redacted ->> 'file_path' AS file_path,
       count(*) AS read_count,
       min(b.occurred_at) AS first_read_at,
       max(b.occurred_at) AS last_read_at
FROM llm_blocks b
WHERE b.block_type = 'tool_use' AND b.tool_name = 'Read'
GROUP BY b.job_id, b.agent_run_id, b.claude_session_id, b.content_json_redacted ->> 'file_path';

-- One chronological stream of spans and events (stage from the agent run when the row was
-- written while its leg was still running).
CREATE VIEW job_forensic_timeline_v AS
SELECT s.job_id, s.started_at AS at, 'span' AS kind, s.span_kind AS subtype, s.name,
       coalesce(s.stage, ar.stage) AS stage, s.transport_status AS status,
       s.semantic_outcome AS outcome, s.duration_ms, s.attributes_redacted AS details
FROM trace_spans s
LEFT JOIN agent_runs ar ON ar.agent_run_id = s.agent_run_id
UNION ALL
SELECT e.job_id, e.occurred_at, 'event', e.source_service, e.event_name,
       coalesce(e.stage, ar.stage), e.severity, e.outcome, NULL::bigint, e.attributes_redacted
FROM runtime_events e
LEFT JOIN agent_runs ar ON ar.agent_run_id = e.agent_run_id;

-- Where the wall time went.
CREATE VIEW job_time_breakdown_v AS
SELECT job_id,
       max(duration_ms) FILTER (WHERE span_kind = 'job') AS job_wall_ms,
       sum(duration_ms) FILTER (WHERE span_kind = 'agent_leg') AS agent_leg_ms,
       sum(duration_ms) FILTER (WHERE span_kind = 'gap'
                                AND semantic_outcome = 'likely_human_gate') AS gate_wait_ms,
       sum(duration_ms) FILTER (WHERE span_kind = 'gap'
                                AND semantic_outcome <> 'likely_human_gate') AS other_gap_ms,
       sum(duration_ms) FILTER (WHERE span_kind = 'llm_inference') AS model_time_ms,
       sum(duration_ms) FILTER (WHERE span_kind = 'tool_call'
                                AND source_service = 'higgsfield_mcp') AS higgsfield_call_ms,
       sum(duration_ms) FILTER (WHERE span_kind IN ('tool_call', 'render', 'qa')
                                AND source_service = 'openmontage_tool') AS local_tool_ms
FROM trace_spans
GROUP BY job_id;

-- ---------------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------------

GRANT SELECT, INSERT ON trace_spans, span_links, llm_inferences, llm_blocks, runtime_events
TO om_phase1_writer;
GRANT UPDATE (trace_id, options_redacted, inputs_redacted) ON jobs TO om_phase1_writer;
GRANT UPDATE (selected, artifact_status, rejected_reason_redacted, expires_at,
              metadata_redacted, stage, scene_id, source_tool, provider, provider_job_id, model,
              mime_type, duration_seconds, width_pixels, height_pixels, fps, producing_span_id)
    ON artifacts TO om_phase1_writer;

GRANT SELECT ON trace_spans, span_links, llm_inferences, llm_blocks, runtime_events,
    tool_calls_v, failed_interactions_v, provider_job_history_v, context_reads_v,
    job_forensic_timeline_v, job_time_breakdown_v
TO om_phase1_reader, om_phase1_writer;

COMMIT;
