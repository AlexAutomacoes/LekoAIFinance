const initSqlJs = require('sql.js');
const path = require('path');
const fs = require('fs');

const DATA_DIR = path.join(__dirname, 'data');
const DB_PATH = path.join(DATA_DIR, 'chamados.db');

// Auto-create data directory
if (!fs.existsSync(DATA_DIR)) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

let db = null;
let sqliteDb = null;

/**
 * Wrapper that exposes a better-sqlite3-like sync API over sql.js.
 * sql.js is pure-JS (no native compilation), so it works without Visual Studio.
 */
class DbWrapper {
  constructor(sqlDb) {
    this.sqlDb = sqlDb;
  }

  prepare(sql) {
    const self = this;
    return {
      run(...params) {
        if (params.length === 1 && typeof params[0] === 'object' && !Array.isArray(params[0])) {
          // Named parameters: replace @name with $name for sql.js
          const obj = params[0];
          const mapped = {};
          for (const [key, value] of Object.entries(obj)) {
            mapped[`$${key}`] = value;
          }
          // Replace @param with $param in SQL
          const adjustedSql = sql.replace(/@(\w+)/g, '$$$1');
          self.sqlDb.run(adjustedSql, mapped);
        } else {
          self.sqlDb.run(sql, params);
        }
        self._save();
        return {
          lastInsertRowid: self.sqlDb.exec("SELECT last_insert_rowid() as id")[0]?.values[0][0],
          changes: self.sqlDb.getRowsModified()
        };
      },
      get(...params) {
        let stmt;
        if (params.length === 1 && typeof params[0] === 'object' && !Array.isArray(params[0])) {
          const obj = params[0];
          const mapped = {};
          for (const [key, value] of Object.entries(obj)) {
            mapped[`$${key}`] = value;
          }
          const adjustedSql = sql.replace(/@(\w+)/g, '$$$1');
          stmt = self.sqlDb.prepare(adjustedSql);
          stmt.bind(mapped);
        } else {
          stmt = self.sqlDb.prepare(sql);
          if (params.length > 0) stmt.bind(params);
        }
        if (stmt.step()) {
          const cols = stmt.getColumnNames();
          const vals = stmt.get();
          stmt.free();
          const row = {};
          cols.forEach((col, i) => { row[col] = vals[i]; });
          return row;
        }
        stmt.free();
        return undefined;
      },
      all(...params) {
        let results;
        if (params.length === 1 && typeof params[0] === 'object' && !Array.isArray(params[0])) {
          const obj = params[0];
          const mapped = {};
          for (const [key, value] of Object.entries(obj)) {
            mapped[`$${key}`] = value;
          }
          const adjustedSql = sql.replace(/@(\w+)/g, '$$$1');
          const stmt = self.sqlDb.prepare(adjustedSql);
          stmt.bind(mapped);
          results = [];
          const cols = stmt.getColumnNames();
          while (stmt.step()) {
            const vals = stmt.get();
            const row = {};
            cols.forEach((col, i) => { row[col] = vals[i]; });
            results.push(row);
          }
          stmt.free();
        } else {
          const stmt = self.sqlDb.prepare(sql);
          if (params.length > 0) stmt.bind(params);
          results = [];
          const cols = stmt.getColumnNames();
          while (stmt.step()) {
            const vals = stmt.get();
            const row = {};
            cols.forEach((col, i) => { row[col] = vals[i]; });
            results.push(row);
          }
          stmt.free();
        }
        return results;
      }
    };
  }

  exec(sql) {
    this.sqlDb.run(sql);
    this._save();
  }

  pragma(str) {
    try {
      this.sqlDb.run(`PRAGMA ${str}`);
    } catch (e) {
      // Ignore unsupported pragmas in sql.js
    }
  }

  _save() {
    const data = this.sqlDb.export();
    const buffer = Buffer.from(data);
    fs.writeFileSync(DB_PATH, buffer);
  }
}

// Synchronous initialization using sql.js's synchronous API
function initSync() {
  // sql.js can be loaded synchronously if we use the wasm file bundled in node_modules
  const SQL = require('sql.js');

  // SQL() returns a promise, but we need sync init for module.exports
  // Use a workaround: check if DB file exists and load it
  return null; // We'll use async init instead
}

// We need async init, so we export a promise that resolves to the wrapper
let dbReady = null;
let wrapper = null;

async function getDb() {
  if (wrapper) return wrapper;
  const SQL = await initSqlJs();
  let sqlDb;
  if (fs.existsSync(DB_PATH)) {
    const fileBuffer = fs.readFileSync(DB_PATH);
    sqlDb = new SQL.Database(fileBuffer);
  } else {
    sqlDb = new SQL.Database();
  }
  wrapper = new DbWrapper(sqlDb);

  // Enable WAL mode (may not apply in sql.js but harmless)
  wrapper.pragma('journal_mode = WAL');

  // Create table if not exists
  wrapper.exec(`
    CREATE TABLE IF NOT EXISTS chamados (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      type TEXT NOT NULL CHECK(type IN ('erro','latencia','melhoria','status')),
      title TEXT NOT NULL,
      description TEXT,
      test_name TEXT,
      latency_ms REAL,
      status TEXT NOT NULL DEFAULT 'aberto' CHECK(status IN ('aberto','resolvido','ignorado')),
      resolution_note TEXT,
      timestamp TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now'))
    );
  `);

  return wrapper;
}

module.exports = { getDb };
