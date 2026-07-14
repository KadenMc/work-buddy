BEGIN TRANSACTION;
CREATE TABLE _migration_history (
    version     INTEGER PRIMARY KEY,
    description TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,            -- ISO UTC (datetime('now'))
    code_hash   TEXT    NOT NULL,            -- sha256, format identified by hash_format
    hash_format TEXT                         -- e.g. 'bytecode_v1'; NULL == legacy
);
INSERT INTO "_migration_history" VALUES(1,'initial truth ledger schema','2026-07-14 20:57:14','bf0ea02a31795a56acec147a309a61a03e4fc21f4f4ae0a05b7f72d69a38422e','ast_v1');
CREATE TABLE claim_links (
            id                       TEXT PRIMARY KEY,
            from_claim_id            TEXT NOT NULL REFERENCES claims(id),
            link_type                TEXT NOT NULL,
            to_kind                  TEXT NOT NULL,
            to_ref                   TEXT NOT NULL,
            role_json                TEXT,
            target_fingerprint       TEXT,
            fingerprint_reviewed_at  TEXT,
            created_at               TEXT NOT NULL,
            created_by_kind          TEXT NOT NULL,
            created_by_ref           TEXT
        );
CREATE TABLE claim_status_events (
            seq         INTEGER PRIMARY KEY AUTOINCREMENT,
            id          TEXT NOT NULL UNIQUE,
            claim_id    TEXT NOT NULL REFERENCES claims(id),
            status      TEXT NOT NULL,
            at          TEXT NOT NULL,
            actor_kind  TEXT NOT NULL,
            actor_ref   TEXT,
            basis_kind  TEXT NOT NULL,
            basis_ref   TEXT,
            note        TEXT
        );
INSERT INTO "claim_status_events" VALUES(1,'f1000000000040008000000000000012','f1000000000040008000000000000011','proposed','2026-07-14T09:00:00Z','human','fixture-human','rule','f1000000000040008000000000000011',NULL);
INSERT INTO "claim_status_events" VALUES(2,'f1000000000040008000000000000014','f1000000000040008000000000000011','confirmed','2026-07-14T09:01:00Z','human','fixture-human','gesture','f1000000000040008000000000000013',NULL);
CREATE TABLE claims (
            id                     TEXT PRIMARY KEY,
            proposition            TEXT NOT NULL,
            canonical_sha256       TEXT NOT NULL,
            claim_kind             TEXT NOT NULL,
            structured_json        TEXT,
            scope                  TEXT NOT NULL DEFAULT 'store',
            valid_from             TEXT,
            valid_to               TEXT,
            confidence_extraction  REAL,
            meta_json              TEXT,
            redacted_at            TEXT,
            created_at             TEXT NOT NULL,
            created_by_kind        TEXT NOT NULL,
            created_by_ref         TEXT
        );
INSERT INTO "claims" VALUES('f1000000000040008000000000000011','Frozen v1 identity and history survive every migration.','6dc84fd34aaf93f6eb80ed589b828d7fdb0f512e60248bce340c805d32dc5db7','decision_outcome','{"decision":"freeze_schema_v1","outcome":"preserve_identity_and_history"}','store',NULL,NULL,NULL,NULL,NULL,'2026-07-14T09:00:00Z','human','fixture-human');
CREATE TABLE claims_current (
            claim_id             TEXT PRIMARY KEY REFERENCES claims(id),
            status               TEXT NOT NULL,
            status_seq           INTEGER NOT NULL,
            effective_valid_from TEXT,
            effective_valid_to   TEXT,
            health               TEXT NOT NULL DEFAULT 'clean',
            health_reason        TEXT,
            rebuilt_at           TEXT NOT NULL
        );
INSERT INTO "claims_current" VALUES('f1000000000040008000000000000011','confirmed',2,NULL,NULL,'clean',NULL,'2026-07-14T09:02:00.000Z');
CREATE TABLE derivation_premises (
            derivation_id  TEXT NOT NULL REFERENCES derivations(id),
            premise_kind   TEXT NOT NULL,
            premise_ref    TEXT NOT NULL,
            PRIMARY KEY (derivation_id, premise_ref)
        );
CREATE TABLE derivations (
            id             TEXT PRIMARY KEY,
            claim_id       TEXT NOT NULL REFERENCES claims(id),
            method         TEXT NOT NULL,
            producer_kind  TEXT NOT NULL,
            producer_ref   TEXT,
            confidence     REAL,
            rationale      TEXT,
            created_at     TEXT NOT NULL
        );
CREATE TABLE evidence (
            id                  TEXT PRIMARY KEY,
            kind                TEXT NOT NULL,
            source_locator      TEXT NOT NULL,
            content_sha256      TEXT NOT NULL,
            content             TEXT,
            content_path        TEXT,
            media_type          TEXT,
            acquired_at         TEXT NOT NULL,
            acquired_by_kind    TEXT NOT NULL,
            acquired_by_ref     TEXT,
            acquisition_method  TEXT NOT NULL,
            trust_class         TEXT NOT NULL,
            derived_from_store  TEXT,
            meta_json           TEXT,
            redacted_at         TEXT,
            created_at          TEXT NOT NULL
        );
CREATE TABLE evidence_spans (
            id               TEXT PRIMARY KEY,
            evidence_id      TEXT NOT NULL REFERENCES evidence(id),
            selector_json    TEXT NOT NULL,
            quote_exact      TEXT,
            span_sha256      TEXT NOT NULL,
            author_kind      TEXT,
            author_ref       TEXT,
            redacted_at      TEXT,
            created_at       TEXT NOT NULL,
            created_by_kind  TEXT NOT NULL,
            created_by_ref   TEXT
        );
CREATE TABLE gestures (
            id              TEXT PRIMARY KEY,
            at              TEXT NOT NULL,
            surface         TEXT NOT NULL,
            actor_ref       TEXT NOT NULL,
            kind            TEXT NOT NULL,
            subject_ref     TEXT NOT NULL,
            payload_sha256  TEXT NOT NULL,
            payload_excerpt TEXT NOT NULL,
            context_sha256  TEXT,
            expires_at      TEXT,
            consumed_at     TEXT
        );
INSERT INTO "gestures" VALUES('f1000000000040008000000000000013','2026-07-14T09:01:00Z','cli','fixture-human','confirm','f1000000000040008000000000000011','6dc84fd34aaf93f6eb80ed589b828d7fdb0f512e60248bce340c805d32dc5db7','Frozen v1 identity and history survive every migration.',NULL,NULL,'2026-07-14T09:01:00Z');
CREATE TABLE ledger_records (
            seq          INTEGER PRIMARY KEY AUTOINCREMENT,
            record_type  TEXT NOT NULL,
            record_key   TEXT NOT NULL,
            UNIQUE (record_type, record_key)
        );
INSERT INTO "ledger_records" VALUES(1,'claim','f1000000000040008000000000000011');
INSERT INTO "ledger_records" VALUES(2,'claim_status_event','f1000000000040008000000000000012');
INSERT INTO "ledger_records" VALUES(3,'gesture','f1000000000040008000000000000013');
INSERT INTO "ledger_records" VALUES(4,'claim_status_event','f1000000000040008000000000000014');
CREATE TABLE link_retractions (
            link_id     TEXT PRIMARY KEY REFERENCES claim_links(id),
            at          TEXT NOT NULL,
            actor_kind  TEXT NOT NULL,
            actor_ref   TEXT,
            reason      TEXT
        );
CREATE TABLE projections (
            id              TEXT PRIMARY KEY,
            path            TEXT NOT NULL,
            rendered_at     TEXT NOT NULL,
            content_sha256  TEXT NOT NULL,
            manifest_json   TEXT NOT NULL,
            health          TEXT NOT NULL DEFAULT 'clean',
            health_reason   TEXT
        );
CREATE TABLE redaction_events (
            id            TEXT PRIMARY KEY,
            subject_kind  TEXT NOT NULL,
            subject_ref   TEXT NOT NULL,
            at            TEXT NOT NULL,
            actor_ref     TEXT NOT NULL,
            basis_kind    TEXT NOT NULL,
            basis_ref     TEXT NOT NULL,
            reason        TEXT NOT NULL
        );
CREATE TABLE store_info (
            store_id       TEXT PRIMARY KEY,
            profile        TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            title          TEXT,
            created_at     TEXT NOT NULL
        );
INSERT INTO "store_info" VALUES('f1000000000040008000000000000001','project-canon',1,'Checked-in frozen truth schema v1','2026-07-14T20:57:14.041+00:00');
CREATE TABLE sweep_findings (
            id                TEXT PRIMARY KEY,
            sweep_id          TEXT NOT NULL REFERENCES sweeps(id),
            subject_kind      TEXT NOT NULL,
            subject_ref       TEXT NOT NULL,
            finding           TEXT NOT NULL,
            resolved_at       TEXT,
            resolved_by_ref   TEXT
        );
CREATE TABLE sweeps (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            at           TEXT NOT NULL,
            params_json  TEXT
        );
CREATE INDEX idx_claim_status_claim_at ON claim_status_events(claim_id, at DESC);
CREATE INDEX idx_claim_status_claim_seq ON claim_status_events(claim_id, seq DESC);
CREATE UNIQUE INDEX uq_claim_status_confirm_gesture ON claim_status_events(basis_ref) WHERE status = 'confirmed' AND basis_kind = 'gesture' AND basis_ref IS NOT NULL;
CREATE INDEX idx_claim_links_from ON claim_links(from_claim_id);
CREATE INDEX idx_claim_links_target ON claim_links(to_kind, to_ref);
CREATE INDEX idx_claims_scope_kind ON claims(scope, claim_kind);
CREATE INDEX idx_claims_scope_valid_from ON claims(scope, valid_from DESC);
CREATE INDEX idx_claims_canonical_sha256 ON claims(canonical_sha256);
CREATE INDEX idx_evidence_content_sha256 ON evidence(content_sha256);
CREATE INDEX idx_evidence_spans_evidence ON evidence_spans(evidence_id);
CREATE INDEX idx_sweep_findings_sweep ON sweep_findings(sweep_id);
CREATE TRIGGER store_info_single_row_insert
        BEFORE INSERT ON store_info
        WHEN EXISTS (SELECT 1 FROM store_info)
        BEGIN
            SELECT RAISE(ABORT, 'store-info-single-row');
        END;
CREATE TRIGGER store_info_append_only_update
        BEFORE UPDATE ON store_info
        WHEN NOT (
            NEW.schema_version > OLD.schema_version
            AND NEW.store_id IS OLD.store_id
            AND NEW.profile IS OLD.profile
            AND NEW.title IS OLD.title
            AND NEW.created_at IS OLD.created_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END;
CREATE TRIGGER evidence_append_only_update
        BEFORE UPDATE ON evidence
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.content IS NULL
            AND NEW.content_path IS NULL
            AND NEW.id IS OLD.id
            AND NEW.kind IS OLD.kind
            AND NEW.source_locator IS OLD.source_locator
            AND NEW.content_sha256 IS OLD.content_sha256
            AND NEW.media_type IS OLD.media_type
            AND NEW.acquired_at IS OLD.acquired_at
            AND NEW.acquired_by_kind IS OLD.acquired_by_kind
            AND NEW.acquired_by_ref IS OLD.acquired_by_ref
            AND NEW.acquisition_method IS OLD.acquisition_method
            AND NEW.trust_class IS OLD.trust_class
            AND NEW.derived_from_store IS OLD.derived_from_store
            AND NEW.meta_json IS OLD.meta_json
            AND NEW.created_at IS OLD.created_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END;
CREATE TRIGGER evidence_spans_append_only_update
        BEFORE UPDATE ON evidence_spans
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.quote_exact IS NULL
            AND NEW.selector_json = '[{"exact":"[redacted]","prefix":"","suffix":"","type":"TextQuoteSelector"}]'
            AND NEW.id IS OLD.id
            AND NEW.evidence_id IS OLD.evidence_id
            AND NEW.span_sha256 IS OLD.span_sha256
            AND NEW.author_kind IS OLD.author_kind
            AND NEW.author_ref IS OLD.author_ref
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END;
CREATE TRIGGER claims_append_only_update
        BEFORE UPDATE ON claims
        WHEN NOT (
            OLD.redacted_at IS NULL
            AND NEW.redacted_at IS NOT NULL
            AND NEW.proposition = '[redacted]'
            AND NEW.structured_json IS NULL
            AND NEW.id IS OLD.id
            AND NEW.canonical_sha256 IS OLD.canonical_sha256
            AND NEW.claim_kind IS OLD.claim_kind
            AND NEW.scope IS OLD.scope
            AND NEW.valid_from IS OLD.valid_from
            AND NEW.valid_to IS OLD.valid_to
            AND NEW.confidence_extraction IS OLD.confidence_extraction
            AND NEW.meta_json IS OLD.meta_json
            AND NEW.created_at IS OLD.created_at
            AND NEW.created_by_kind IS OLD.created_by_kind
            AND NEW.created_by_ref IS OLD.created_by_ref
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END;
CREATE TRIGGER gestures_append_only_update
        BEFORE UPDATE ON gestures
        WHEN NOT (
            NEW.id IS OLD.id
            AND NEW.at IS OLD.at
            AND NEW.surface IS OLD.surface
            AND NEW.actor_ref IS OLD.actor_ref
            AND NEW.kind IS OLD.kind
            AND NEW.subject_ref IS OLD.subject_ref
            AND NEW.payload_sha256 IS OLD.payload_sha256
            AND NEW.context_sha256 IS OLD.context_sha256
            AND NEW.expires_at IS OLD.expires_at
            AND (
                (
                    OLD.consumed_at IS NULL
                    AND NEW.consumed_at IS NOT NULL
                    AND NEW.payload_excerpt IS OLD.payload_excerpt
                )
                OR (
                    NEW.consumed_at IS OLD.consumed_at
                    AND OLD.payload_excerpt <> '[redacted]'
                    AND NEW.payload_excerpt = '[redacted]'
                    AND (
                        EXISTS (
                            SELECT 1 FROM claims
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                        OR EXISTS (
                            SELECT 1 FROM evidence
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                        OR EXISTS (
                            SELECT 1 FROM evidence_spans
                            WHERE id = OLD.subject_ref
                            AND redacted_at IS NOT NULL
                        )
                    )
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END;
CREATE TRIGGER sweep_findings_append_only_update
        BEFORE UPDATE ON sweep_findings
        WHEN NOT (
            OLD.resolved_at IS NULL
            AND NEW.resolved_at IS NOT NULL
            AND NEW.id IS OLD.id
            AND NEW.sweep_id IS OLD.sweep_id
            AND NEW.subject_kind IS OLD.subject_kind
            AND NEW.subject_ref IS OLD.subject_ref
            AND NEW.finding IS OLD.finding
        )
        BEGIN
            SELECT RAISE(ABORT, 'append-only');
        END;
CREATE TRIGGER ledger_records_append_only_update
            BEFORE UPDATE ON ledger_records
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER derivations_append_only_update
            BEFORE UPDATE ON derivations
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER derivation_premises_append_only_update
            BEFORE UPDATE ON derivation_premises
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER claim_links_append_only_update
            BEFORE UPDATE ON claim_links
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER link_retractions_append_only_update
            BEFORE UPDATE ON link_retractions
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER claim_status_events_append_only_update
            BEFORE UPDATE ON claim_status_events
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER redaction_events_append_only_update
            BEFORE UPDATE ON redaction_events
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER sweeps_append_only_update
            BEFORE UPDATE ON sweeps
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER store_info_append_only_delete
            BEFORE DELETE ON store_info
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER ledger_records_append_only_delete
            BEFORE DELETE ON ledger_records
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER evidence_append_only_delete
            BEFORE DELETE ON evidence
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER evidence_spans_append_only_delete
            BEFORE DELETE ON evidence_spans
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER claims_append_only_delete
            BEFORE DELETE ON claims
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER derivations_append_only_delete
            BEFORE DELETE ON derivations
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER derivation_premises_append_only_delete
            BEFORE DELETE ON derivation_premises
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER claim_links_append_only_delete
            BEFORE DELETE ON claim_links
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER link_retractions_append_only_delete
            BEFORE DELETE ON link_retractions
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER claim_status_events_append_only_delete
            BEFORE DELETE ON claim_status_events
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER gestures_append_only_delete
            BEFORE DELETE ON gestures
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER redaction_events_append_only_delete
            BEFORE DELETE ON redaction_events
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER sweeps_append_only_delete
            BEFORE DELETE ON sweeps
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
CREATE TRIGGER sweep_findings_append_only_delete
            BEFORE DELETE ON sweep_findings
            BEGIN
                SELECT RAISE(ABORT, 'append-only');
            END;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('ledger_records',4);
INSERT INTO "sqlite_sequence" VALUES('claim_status_events',2);
COMMIT;
PRAGMA user_version=1;

