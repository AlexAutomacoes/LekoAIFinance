const express = require('express');
const cors = require('cors');
const { getDb } = require('./db');
const chamadosRouter = require('./routes/chamados');

const app = express();
const PORT = 3847;
const HOST = '127.0.0.1';

// CORS - allow localhost origins
app.use(cors({
  origin: (origin, callback) => {
    if (!origin || /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)) {
      callback(null, true);
    } else {
      callback(new Error('Not allowed by CORS'));
    }
  }
}));

// JSON body parsing
app.use(express.json());

// Request logging
app.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    const duration = Date.now() - start;
    console.log(`${req.method} ${req.url} ${res.statusCode} ${duration}ms`);
  });
  next();
});

// Routes
app.use('/api/chamados', chamadosRouter);

// GET /api/stats - Summary statistics
app.get('/api/stats', async (req, res) => {
  try {
    const db = await getDb();

    const total = db.prepare('SELECT COUNT(*) as count FROM chamados').get().count;

    const byType = db.prepare(`
      SELECT type, COUNT(*) as count FROM chamados GROUP BY type
    `).all().reduce((acc, row) => {
      acc[row.type] = row.count;
      return acc;
    }, { erro: 0, latencia: 0, melhoria: 0, status: 0 });

    const byStatus = db.prepare(`
      SELECT status, COUNT(*) as count FROM chamados GROUP BY status
    `).all().reduce((acc, row) => {
      acc[row.status] = row.count;
      return acc;
    }, { aberto: 0, resolvido: 0, ignorado: 0 });

    const avgLatency = db.prepare(
      'SELECT AVG(latency_ms) as avg FROM chamados WHERE latency_ms IS NOT NULL'
    ).get().avg;

    const lastRun = db.prepare(
      'SELECT timestamp FROM chamados ORDER BY created_at DESC LIMIT 1'
    ).get();

    res.json({
      total,
      by_type: byType,
      by_status: byStatus,
      avg_latency_ms: avgLatency ? Math.round(avgLatency * 100) / 100 : null,
      last_run: lastRun ? lastRun.timestamp : null
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 404 handler
app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

// Error handler
app.use((err, req, res, next) => {
  console.error(err.stack);
  res.status(500).json({ error: 'Internal server error' });
});

// Initialize DB then start server
async function start() {
  await getDb(); // ensure DB is ready
  app.listen(PORT, HOST, () => {
    console.log(`LekoAIFinance Dashboard API running at http://${HOST}:${PORT}`);
  });
}

start().catch(err => {
  console.error('Failed to start server:', err);
  process.exit(1);
});
