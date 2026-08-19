-- Criar esta tabela no Supabase SQL Editor
-- Dashboard > SQL Editor > New query > Cole e execute

CREATE TABLE IF NOT EXISTS chamados (
  id BIGSERIAL PRIMARY KEY,
  type TEXT NOT NULL CHECK (type IN ('erro', 'latencia', 'melhoria', 'status')),
  title TEXT NOT NULL,
  description TEXT,
  test_name TEXT,
  latency_ms REAL,
  status TEXT NOT NULL DEFAULT 'aberto' CHECK (status IN ('aberto', 'resolvido', 'ignorado')),
  resolution_note TEXT,
  timestamp TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes para queries comuns
CREATE INDEX IF NOT EXISTS idx_chamados_type ON chamados(type);
CREATE INDEX IF NOT EXISTS idx_chamados_status ON chamados(status);
CREATE INDEX IF NOT EXISTS idx_chamados_timestamp ON chamados(timestamp DESC);

-- Habilitar RLS (Row Level Security) - acesso apenas via service key
ALTER TABLE chamados ENABLE ROW LEVEL SECURITY;

-- Policy: permite tudo via service_role key (que é a que usamos no backend)
CREATE POLICY "Allow all for service role" ON chamados
  FOR ALL
  USING (true)
  WITH CHECK (true);

-- Permitir acesso anonimo de leitura para o frontend do dashboard
CREATE POLICY "Allow anonymous read" ON chamados
  FOR SELECT
  USING (true);
