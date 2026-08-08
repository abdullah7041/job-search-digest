CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  user_id TEXT NOT NULL,
  job_key TEXT,
  input_json TEXT NOT NULL,
  interaction_token TEXT NOT NULL,
  status TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  delivery_lease_until INTEGER,
  delivery_error TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
  job_key TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  posted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_claims (
  job_key TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('posting', 'retryable', 'posted')),
  message_id TEXT,
  claimed_at TEXT NOT NULL,
  lease_until INTEGER,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS internal_nonces (
  nonce TEXT PRIMARY KEY,
  expires_at TEXT NOT NULL
);

-- Snapshot rows are append-only and keyed by the exact Discord message whose
-- button resolves them. This prevents a later digest from changing an older
-- card's job description.
CREATE TABLE IF NOT EXISTS job_snapshots (
  message_id TEXT PRIMARY KEY,
  job_key TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  captured_at TEXT NOT NULL
);
