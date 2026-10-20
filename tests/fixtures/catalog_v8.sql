-- Catalog schema version 8, written by the version-8 code with synthetic rows.
PRAGMA user_version=8;
BEGIN TRANSACTION;
CREATE TABLE artifact_projections (
        layer TEXT NOT NULL CHECK (layer IN ('journal','document','wiki')),
        artifact_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
        projection_path TEXT NOT NULL, desired_hash TEXT NOT NULL CHECK (length(desired_hash)=64 AND desired_hash NOT GLOB '*[^0-9a-f]*'),
        last_projected_hash TEXT CHECK (last_projected_hash IS NULL OR (length(last_projected_hash)=64 AND last_projected_hash NOT GLOB '*[^0-9a-f]*')),
        state TEXT NOT NULL CHECK (state IN ('not_projected','pending','projected','conflict','failed')),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (state != 'projected' OR (last_projected_hash IS NOT NULL AND last_projected_hash=desired_hash)),
        CHECK (length(projection_path) > 0 AND substr(projection_path,1,1) != '/' AND instr(projection_path,char(92))=0 AND instr(projection_path,'//')=0 AND instr('/'||projection_path||'/','/../')=0 AND instr('/'||projection_path||'/','/./')=0),
        PRIMARY KEY (layer,artifact_id,revision)
    );
CREATE TABLE artifact_publications (
    artifact_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
    projection_path TEXT NOT NULL,
    desired_hash TEXT NOT NULL CHECK (length(desired_hash)=64 AND desired_hash NOT GLOB '*[^0-9a-f]*'),
    device INTEGER CHECK (device IS NULL OR device >= 0), inode INTEGER CHECK (inode IS NULL OR inode > 0),
    staged_name TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('planned','pending','retired','completed')),
    CHECK ((device IS NULL) = (inode IS NULL)),
    CHECK (state != 'pending' OR device IS NOT NULL),
    CHECK (state != 'planned' OR device IS NULL),
    CHECK (length(projection_path) > 0 AND substr(projection_path,1,1) != '/' AND instr(projection_path,char(92))=0 AND instr(projection_path,'//')=0 AND instr('/'||projection_path||'/','/../')=0 AND instr('/'||projection_path||'/','/./')=0),
    PRIMARY KEY (artifact_id,revision)
);
CREATE TABLE categories (
        category_id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        name_key TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 4096),
        aliases_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(aliases_json)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
INSERT INTO "categories" VALUES('inbox','Inbox','inbox','','[]',1,'2026-10-17 19:52:12');
CREATE TABLE category_proposal_documents (
        proposal_id TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        PRIMARY KEY (proposal_id,source_id,source_version),
        FOREIGN KEY (proposal_id) REFERENCES category_proposals(proposal_id),
        FOREIGN KEY (source_id,source_version) REFERENCES document_metadata(source_id,source_version)
    );
CREATE TABLE category_proposals (
        proposal_id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        name_key TEXT NOT NULL, description TEXT NOT NULL CHECK (length(description) <= 4096),
        rationale TEXT NOT NULL CHECK (length(rationale) <= 4096),
        closest_category_id TEXT, category_id TEXT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','rejected','redirected')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (closest_category_id) REFERENCES categories(category_id),
        FOREIGN KEY (category_id) REFERENCES categories(category_id),
        CHECK ((state IN ('approved','redirected')) = (category_id IS NOT NULL))
    );
CREATE TABLE curation_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL CHECK (source_version > 0),
        action TEXT NOT NULL CHECK (action IN (
            'correct', 'reindex', 'prepare_forget', 'forget', 'prepare_delete', 'delete'
        )),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, prepared_operation_id TEXT
                           REFERENCES operations(operation_id),
        FOREIGN KEY (operation_id) REFERENCES operations(operation_id),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    );
CREATE TABLE document_categories (
        source_id TEXT PRIMARY KEY NOT NULL,
        category_id TEXT NOT NULL DEFAULT 'inbox',
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        FOREIGN KEY (source_id) REFERENCES sources(source_id),
        FOREIGN KEY (category_id) REFERENCES categories(category_id)
    );
INSERT INTO "document_categories" VALUES('doc_v8','inbox',1);
CREATE TABLE document_metadata (
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL CHECK (source_version > 0),
        filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 1024),
        mime_type TEXT NOT NULL CHECK (length(mime_type) BETWEEN 3 AND 255),
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id, source_version) REFERENCES source_versions(source_id, source_version)
    );
INSERT INTO "document_metadata" VALUES('doc_v8',1,'v8.txt','Version eight','text/plain');
CREATE TABLE document_stages (
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL CHECK (source_version > 0),
        stage TEXT NOT NULL CHECK (stage IN ('custody','extraction','indexing','wiki','projection')),
        state TEXT NOT NULL CHECK (state IN ('pending','running','accepted','blocked','failed','conflict','completed')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        error_code TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND error_code NOT GLOB '*[^a-z0-9_]*')),
        CHECK (state IN ('pending','running','failed') OR
            (stage='custody' AND state='accepted') OR
            (stage='extraction' AND state IN ('blocked','completed')) OR
            (stage IN ('indexing','wiki','projection') AND state='completed') OR
            (stage IN ('wiki','projection') AND state='conflict')),
        PRIMARY KEY (source_id,source_version,stage),
        FOREIGN KEY (source_id,source_version) REFERENCES document_metadata(source_id,source_version)
    );
INSERT INTO "document_stages" VALUES('doc_v8',1,'custody','accepted',1,0,NULL,'2026-10-17 19:52:12');
INSERT INTO "document_stages" VALUES('doc_v8',1,'extraction','completed',3,1,NULL,'2026-10-17 19:52:12');
INSERT INTO "document_stages" VALUES('doc_v8',1,'indexing','completed',3,1,NULL,'2026-10-17 19:52:12');
INSERT INTO "document_stages" VALUES('doc_v8',1,'wiki','pending',1,0,NULL,'2026-10-17 19:52:12');
INSERT INTO "document_stages" VALUES('doc_v8',1,'projection','pending',1,0,NULL,'2026-10-17 19:52:12');
CREATE TABLE extractions (
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL CHECK (source_version > 0),
        extraction_revision INTEGER NOT NULL CHECK (extraction_revision > 0),
        original_sha256 TEXT NOT NULL CHECK (length(original_sha256)=64 AND original_sha256 NOT GLOB '*[^0-9a-f]*'),
        extractor TEXT NOT NULL CHECK (length(extractor) BETWEEN 1 AND 256),
        extractor_version TEXT NOT NULL CHECK (length(extractor_version) BETWEEN 1 AND 256),
        config_sha256 TEXT NOT NULL CHECK (length(config_sha256)=64 AND config_sha256 NOT GLOB '*[^0-9a-f]*'),
        artifact_path TEXT, artifact_sha256 TEXT CHECK (artifact_sha256 IS NULL OR (length(artifact_sha256)=64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*')),
        status TEXT NOT NULL CHECK (status IN ('completed','blocked','failed','unsupported','locked','limit_exceeded')),
        warnings_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings_json)),
        passage_count INTEGER NOT NULL CHECK (passage_count >= 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK ((artifact_path IS NULL) = (artifact_sha256 IS NULL)),
        CHECK (status != 'completed' OR (artifact_path IS NOT NULL AND passage_count > 0)),
        PRIMARY KEY (source_id,source_version,extraction_revision),
        FOREIGN KEY (source_id,source_version) REFERENCES document_metadata(source_id,source_version)
    );
INSERT INTO "extractions" VALUES('doc_v8',1,1,'9b53291885ec3ee895d0db847f163335480a3cbd3793d5fa25b81209bf49cee1','text','1','cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc','derived/doc_v8/v1/r1.json','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','completed','[]',1,'2026-10-17 19:52:12');
CREATE TABLE journal_projection_moves (
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    old_path TEXT NOT NULL,
    new_path TEXT NOT NULL UNIQUE,
    old_hash TEXT NOT NULL CHECK (length(old_hash)=64 AND old_hash NOT GLOB '*[^0-9a-f]*'),
    new_hash TEXT NOT NULL CHECK (length(new_hash)=64 AND new_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (old_path != new_path),
    PRIMARY KEY (source_id,source_version),
    FOREIGN KEY (source_id,source_version) REFERENCES source_versions(source_id,source_version)
);
CREATE TABLE "operations" (
                        operation_id TEXT PRIMARY KEY NOT NULL CHECK (
                            length(operation_id) = 35
                            AND substr(operation_id, 1, 3) = 'op_'
                            AND substr(operation_id, 4) NOT GLOB '*[^0-9a-f]*'
                        ),
                        idempotency_key_digest TEXT NOT NULL UNIQUE CHECK (
                            length(idempotency_key_digest) = 64
                            AND idempotency_key_digest NOT GLOB '*[^0-9a-f]*'
                        ),
                        request_digest TEXT NOT NULL CHECK (
                            length(request_digest) = 64
                            AND request_digest NOT GLOB '*[^0-9a-f]*'
                        ),
                        operation_kind TEXT NOT NULL CHECK (operation_kind IN ('retain', 'curation', 'relationship_scan', 'relationship_decision', 'import', 'document_import', 'document_retry', 'wiki_rebuild', 'category_decision')),
                        state TEXT NOT NULL CHECK (state IN (
                            'pending', 'running', 'retryable_failed', 'permanent_failed',
                            'completed', 'cancelled'
                        )),
                        result_json TEXT NOT NULL DEFAULT '{}'
                            CHECK (length(CAST(result_json AS BLOB)) <= 4194304),
                        error_code TEXT CHECK (error_code IS NULL OR (
                            length(error_code) BETWEEN 1 AND 64
                            AND error_code NOT GLOB '*[^a-z0-9_]*'
                            AND substr(error_code, 1, 1) GLOB '[a-z]'
                        )),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
INSERT INTO "operations" VALUES('op_e99b002a68694474a76591475235d709','2620316f6f497d557fe71dbe2543dc9ba9b2abae0660eb8ccccba3e4c62353c7','1111111111111111111111111111111111111111111111111111111111111111','retain','completed','{}',NULL,'2026-10-17 19:52:12','2026-10-17 19:52:12');
INSERT INTO "operations" VALUES('op_c862cd1b491743ca89c8a17af7a2b55e','6075588f6ff15024b4c25df7ee190d8e180b48ceb812228c0f6e2d1c63c97844','2222222222222222222222222222222222222222222222222222222222222222','wiki_rebuild','pending','{}',NULL,'2026-10-17 19:52:12','2026-10-17 19:52:12');
CREATE TABLE projection_states (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'not_projected'
            CHECK (state IN ('not_projected', 'pending', 'projected', 'conflict', 'failed')),
        projection_path TEXT,
        desired_hash TEXT,
        last_projected_hash TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    );
INSERT INTO "projection_states" VALUES('src_v8',1,'not_projected',NULL,NULL,NULL,'2026-10-17 19:52:12');
INSERT INTO "projection_states" VALUES('doc_v8',1,'not_projected',NULL,NULL,NULL,'2026-10-17 19:52:12');
CREATE TABLE provider_chunks (
        provider_id TEXT PRIMARY KEY NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 512),
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        extraction_revision INTEGER NOT NULL,
        passage_start INTEGER NOT NULL CHECK (passage_start >= 0),
        passage_end INTEGER NOT NULL CHECK (passage_end > passage_start),
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        submission_state TEXT NOT NULL DEFAULT 'pending' CHECK (submission_state IN ('pending','submitted','failed','retired')),
        completion_state TEXT NOT NULL DEFAULT 'unknown' CHECK (completion_state IN ('unknown','pending','completed','failed')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (completion_state != 'completed' OR submission_state IN ('submitted','retired')),
        UNIQUE (source_id,source_version,extraction_revision,passage_start,passage_end),
        FOREIGN KEY (source_id,source_version,extraction_revision) REFERENCES extractions(source_id,source_version,extraction_revision)
    );
INSERT INTO "provider_chunks" VALUES('chunk_v8','doc_v8',1,1,0,1,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','submitted','completed',2,'2026-10-17 19:52:12');
CREATE TABLE provider_states (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        submission_state TEXT NOT NULL DEFAULT 'pending'
            CHECK (submission_state IN ('pending', 'submitted', 'failed')),
        completion_state TEXT NOT NULL DEFAULT 'unknown'
            CHECK (completion_state IN ('unknown', 'pending', 'completed', 'failed')),
        recall_visibility TEXT NOT NULL DEFAULT 'unknown'
            CHECK (recall_visibility IN ('unknown', 'visible', 'not_visible')),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    );
INSERT INTO "provider_states" VALUES('src_v8',1,'pending','unknown','unknown','2026-10-17 19:52:12');
INSERT INTO "provider_states" VALUES('doc_v8',1,'pending','unknown','unknown','2026-10-17 19:52:12');
CREATE TABLE reconciliation_facts (
        fact_id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        fact TEXT NOT NULL CHECK (fact IN (
            'missing_original', 'corrupt_original', 'stale_projection'
        )),
        expected_hash TEXT,
        actual_hash TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    );
CREATE TABLE source_versions (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL CHECK (source_version > 0),
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        provenance_json TEXT NOT NULL,
        manifest_path TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id) REFERENCES sources(source_id)
    );
INSERT INTO "source_versions" VALUES('src_v8',1,'214dbb6e36f3482b520af12ef7027c16fc01f39f6793dadae701d3d4a74fd608',27,'{"source":"fixture"}','sources/src_v8/v1/manifest.json','{"provenance":{"source":"fixture"},"schema_version":1,"sha256":"214dbb6e36f3482b520af12ef7027c16fc01f39f6793dadae701d3d4a74fd608","size_bytes":27,"source_id":"src_v8","source_version":1}');
INSERT INTO "source_versions" VALUES('doc_v8',1,'9b53291885ec3ee895d0db847f163335480a3cbd3793d5fa25b81209bf49cee1',22,'{"source":"fixture"}','sources/doc_v8/v1/manifest.json','{"provenance":{"source":"fixture"},"schema_version":1,"sha256":"9b53291885ec3ee895d0db847f163335480a3cbd3793d5fa25b81209bf49cee1","size_bytes":22,"source_id":"doc_v8","source_version":1}');
CREATE TABLE sources (
        source_id TEXT PRIMARY KEY,
        lifecycle_state TEXT NOT NULL DEFAULT 'active'
            CHECK (lifecycle_state IN ('active')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    , layer TEXT NOT NULL DEFAULT 'journal' CHECK (layer IN ('journal','document')));
INSERT INTO "sources" VALUES('src_v8','active','2026-10-17 19:52:12','journal');
INSERT INTO "sources" VALUES('doc_v8','active','2026-10-17 19:52:12','document');
CREATE TABLE "tombstones" (
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL CHECK (source_version > 0),
                        tombstone_type TEXT NOT NULL CHECK (tombstone_type IN ('forgotten', 'deleted')),
                        operation_id TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        progress_json TEXT NOT NULL DEFAULT '{"provider_delete":"pending","projection_delete":"pending","custody_delete":"pending"}'
                            CHECK (length(CAST(progress_json AS BLOB)) <= 512),
                        PRIMARY KEY (source_id, source_version),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id),
                        FOREIGN KEY (source_id, source_version)
                            REFERENCES source_versions(source_id, source_version)
                    );
CREATE TABLE transfer_items (
                        operation_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL CHECK (source_version > 0),
                        lifecycle_state TEXT NOT NULL CHECK (
                            lifecycle_state IN ('active', 'forgotten', 'deleted')
                        ),
                        sha256 TEXT CHECK (sha256 IS NULL OR (
                            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
                        )),
                        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
                            'pending', 'custody_written', 'catalog_registered', 'projected',
                            'provider_submitted', 'tombstoned', 'completed'
                        )),
                        PRIMARY KEY (operation_id, source_id, source_version),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                    );
CREATE TABLE transfer_tombstones (
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL CHECK (source_version > 0),
                        tombstone_type TEXT NOT NULL CHECK (
                            tombstone_type IN ('forgotten', 'deleted')
                        ),
                        operation_id TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (source_id, source_version),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                    );
CREATE TABLE transfers (
                        operation_id TEXT PRIMARY KEY NOT NULL,
                        bundle_digest TEXT NOT NULL CHECK (
                            length(bundle_digest) = 64
                            AND bundle_digest NOT GLOB '*[^0-9a-f]*'
                        ),
                        item_count INTEGER NOT NULL CHECK (item_count BETWEEN 0 AND 4096),
                        size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 4194304),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                    );
CREATE TABLE wiki_dependencies (
        page_id TEXT NOT NULL, revision INTEGER NOT NULL,
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL, extraction_revision INTEGER NOT NULL,
        passage_start INTEGER NOT NULL CHECK (passage_start >= 0),
        passage_end INTEGER NOT NULL CHECK (passage_end > passage_start),
        original_sha256 TEXT NOT NULL CHECK (length(original_sha256)=64 AND original_sha256 NOT GLOB '*[^0-9a-f]*'),
        PRIMARY KEY (page_id,revision,source_id,source_version,extraction_revision,passage_start,passage_end),
        FOREIGN KEY (page_id,revision) REFERENCES wiki_revisions(page_id,revision),
        FOREIGN KEY (source_id,source_version,extraction_revision) REFERENCES extractions(source_id,source_version,extraction_revision)
    );
INSERT INTO "wiki_dependencies" VALUES('source_doc_v8',1,'doc_v8',1,1,0,1,'9b53291885ec3ee895d0db847f163335480a3cbd3793d5fa25b81209bf49cee1');
CREATE TABLE wiki_pages (
        page_id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 1024),
        current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','current','stale','conflict')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
INSERT INTO "wiki_pages" VALUES('source_doc_v8','Version eight',1,'conflict','2026-10-17 19:52:12');
CREATE TABLE wiki_reviews (
        review_id TEXT PRIMARY KEY NOT NULL, page_id TEXT NOT NULL, revision INTEGER NOT NULL,
        code TEXT NOT NULL CHECK (code IN ('manual_edit','content_conflict','contradiction')),
        details_json TEXT NOT NULL CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','resolved')),
        FOREIGN KEY (page_id,revision) REFERENCES wiki_revisions(page_id,revision)
    );
INSERT INTO "wiki_reviews" VALUES('review_v8','source_doc_v8',1,'manual_edit','{"actual_hash":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","expected_hash":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}','pending');
CREATE TABLE wiki_revisions (
        page_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
        parent_revision INTEGER CHECK (parent_revision IS NULL OR (parent_revision > 0 AND parent_revision < revision)),
        artifact_path TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL CHECK (length(artifact_sha256)=64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (page_id,revision),
        FOREIGN KEY (page_id) REFERENCES wiki_pages(page_id),
        FOREIGN KEY (page_id,parent_revision) REFERENCES wiki_revisions(page_id,revision)
    );
INSERT INTO "wiki_revisions" VALUES('source_doc_v8',1,NULL,'wiki_revisions/source_doc_v8/r1.json','dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd','2026-10-17 19:52:12');
CREATE INDEX source_versions_hash_idx ON source_versions(sha256);
CREATE INDEX reconciliation_facts_source_idx ON reconciliation_facts(source_id, source_version, fact_id);
CREATE INDEX curation_history_source_idx ON curation_history(source_id, source_version, history_id);
CREATE INDEX curation_history_prepared_idx ON curation_history(prepared_operation_id, history_id);
CREATE INDEX tombstones_operation_idx ON tombstones(operation_id, source_id);
CREATE INDEX transfer_items_state_idx ON transfer_items(operation_id, state, source_id, source_version);
CREATE INDEX transfer_tombstones_operation_idx ON transfer_tombstones(operation_id, source_id);
CREATE INDEX operations_state_created_idx ON operations(state,created_at,operation_id);
CREATE UNIQUE INDEX category_proposals_pending_idx ON category_proposals(name_key) WHERE state='pending';
DELETE FROM "sqlite_sequence";
COMMIT;
