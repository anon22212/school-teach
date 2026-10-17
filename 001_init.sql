CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'teacher' CHECK (role IN ('teacher','admin')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  created_at TEXT NOT NULL,
  last_login_at TEXT,
  last_active_at TEXT
);
CREATE TABLE generations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK (type IN ('control','independent','check','test','lesson')),
  subject TEXT NOT NULL,
  grade INTEGER NOT NULL,
  class_letter TEXT NOT NULL,
  topic TEXT NOT NULL,
  difficulty TEXT,
  variants_count INTEGER,
  tasks_count INTEGER,
  duration_minutes INTEGER,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','success','error')),
  request_data TEXT NOT NULL,
  prompt_text TEXT,
  result_data TEXT,
  error_message TEXT,
  model_name TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  estimated_cost REAL,
  duration_ms INTEGER,
  request_token TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX idx_gen_created ON generations(created_at);
CREATE INDEX idx_gen_user ON generations(user_id, created_at);
CREATE INDEX idx_gen_filter ON generations(subject, grade, class_letter);
CREATE INDEX idx_gen_status ON generations(status);
CREATE INDEX idx_gen_type ON generations(type);
CREATE INDEX idx_gen_topic ON generations(topic COLLATE NOCASE);
CREATE UNIQUE INDEX idx_gen_token ON generations(user_id, request_token) WHERE request_token IS NOT NULL;

CREATE TABLE saved_materials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  generation_id INTEGER REFERENCES generations(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_mat_user ON saved_materials(user_id, updated_at);
CREATE INDEX idx_mat_gen ON saved_materials(generation_id);

CREATE TABLE material_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id INTEGER NOT NULL REFERENCES saved_materials(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_ver_mat ON material_versions(material_id, created_at);

CREATE TABLE shared_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id INTEGER NOT NULL UNIQUE REFERENCES saved_materials(id) ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE test_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shared_test_id INTEGER NOT NULL REFERENCES shared_tests(id) ON DELETE CASCADE,
  student_name TEXT NOT NULL,
  variant INTEGER NOT NULL,
  answers TEXT NOT NULL,
  score REAL NOT NULL,
  max_score REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE password_resets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE admin_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id INTEGER,
  action TEXT NOT NULL,
  target TEXT,
  created_at TEXT NOT NULL
);
