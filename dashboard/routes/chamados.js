const { Router } = require('express');
const { getDb } = require('../db');

const router = Router();

// POST /api/chamados - Create a new chamado
router.post('/', async (req, res) => {
  const { type, title, description, test_name, latency_ms, status, timestamp } = req.body;

  if (!type || !title || !timestamp) {
    return res.status(400).json({ error: 'Missing required fields: type, title, timestamp' });
  }

  const validTypes = ['erro', 'latencia', 'melhoria', 'status'];
  if (!validTypes.includes(type)) {
    return res.status(400).json({ error: `Invalid type. Must be one of: ${validTypes.join(', ')}` });
  }

  const validStatuses = ['aberto', 'resolvido', 'ignorado'];
  const finalStatus = status || 'aberto';
  if (!validStatuses.includes(finalStatus)) {
    return res.status(400).json({ error: `Invalid status. Must be one of: ${validStatuses.join(', ')}` });
  }

  try {
    const db = await getDb();
    const result = db.prepare(`
      INSERT INTO chamados (type, title, description, test_name, latency_ms, status, timestamp)
      VALUES (@type, @title, @description, @test_name, @latency_ms, @status, @timestamp)
    `).run({
      type,
      title,
      description: description || null,
      test_name: test_name || null,
      latency_ms: latency_ms ?? null,
      status: finalStatus,
      timestamp
    });

    const created = db.prepare('SELECT * FROM chamados WHERE id = ?').get(result.lastInsertRowid);
    res.status(201).json(created);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/chamados - List chamados with optional filters
router.get('/', async (req, res) => {
  const { type, status, limit = 50, offset = 0 } = req.query;

  let query = 'SELECT * FROM chamados WHERE 1=1';
  const params = {};

  if (type) {
    query += ' AND type = @type';
    params.type = type;
  }
  if (status) {
    query += ' AND status = @status';
    params.status = status;
  }

  query += ' ORDER BY created_at DESC LIMIT @limit OFFSET @offset';
  params.limit = parseInt(limit, 10);
  params.offset = parseInt(offset, 10);

  try {
    const db = await getDb();
    const rows = db.prepare(query).all(params);
    res.json(rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/chamados/:id - Get single chamado
router.get('/:id', async (req, res) => {
  const { id } = req.params;

  try {
    const db = await getDb();
    const row = db.prepare('SELECT * FROM chamados WHERE id = ?').get(parseInt(id, 10));
    if (!row) {
      return res.status(404).json({ error: 'Chamado not found' });
    }
    res.json(row);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// PATCH /api/chamados/:id - Update status
router.patch('/:id', async (req, res) => {
  const { id } = req.params;
  const { status, resolution_note } = req.body;

  if (!status) {
    return res.status(400).json({ error: 'Missing required field: status' });
  }

  const validStatuses = ['resolvido', 'ignorado'];
  if (!validStatuses.includes(status)) {
    return res.status(400).json({ error: `Invalid status for update. Must be one of: ${validStatuses.join(', ')}` });
  }

  try {
    const db = await getDb();
    const existing = db.prepare('SELECT * FROM chamados WHERE id = ?').get(parseInt(id, 10));
    if (!existing) {
      return res.status(404).json({ error: 'Chamado not found' });
    }

    db.prepare(`
      UPDATE chamados SET status = @status, resolution_note = @resolution_note WHERE id = @id
    `).run({
      status,
      resolution_note: resolution_note || null,
      id: parseInt(id, 10)
    });

    const updated = db.prepare('SELECT * FROM chamados WHERE id = ?').get(parseInt(id, 10));
    res.json(updated);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// DELETE /api/chamados/:id - Delete a chamado
router.delete('/:id', async (req, res) => {
  const { id } = req.params;

  try {
    const db = await getDb();
    const existing = db.prepare('SELECT * FROM chamados WHERE id = ?').get(parseInt(id, 10));
    if (!existing) {
      return res.status(404).json({ error: 'Chamado not found' });
    }

    db.prepare('DELETE FROM chamados WHERE id = ?').run(parseInt(id, 10));
    res.status(204).send();
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
