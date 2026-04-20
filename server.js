const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const os = require('os');
const multer = require('multer');
const FormData = require('form-data');
const axios = require('axios');
const archiver = require('archiver');
const { kml } = require('@tmcw/togeojson');
const { DOMParser } = require('xmldom');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const sqlite3 = require('sqlite3').verbose();

const { exec } = require('child_process');
const util = require('util');
const execPromise = util.promisify(exec);

const app = express();
const PORT = process.env.PORT || 9008;
const JWT_SECRET = process.env.JWT_SECRET || 'kml_secret_key_2026';
const HARDCODED_TEST_USER = 'test';
const HARDCODED_TEST_PASSWORD = '123';
app.set('trust proxy', true);

// Define directories first
const DATA_DIR = process.env.DATA_DIR
    ? path.resolve(process.env.DATA_DIR)
    : path.join(__dirname, 'data');
const USERS_FILE = path.join(DATA_DIR, 'users.json');
const IMAGE_DB_FILE = path.join(DATA_DIR, 'kml_images.db');
let imageDb = null;

function openImageDb() {
    if (imageDb) return imageDb;
    imageDb = new sqlite3.Database(IMAGE_DB_FILE, (err) => {
        if (err) {
            console.error('[DB] Failed to open SQLite DB:', err);
        } else {
            console.log(`[DB] SQLite ready: ${IMAGE_DB_FILE}`);
        }
    });
    return imageDb;
}

function dbRun(sql, params = []) {
    const db = openImageDb();
    return new Promise((resolve, reject) => {
        db.run(sql, params, function onRun(err) {
            if (err) return reject(err);
            resolve(this);
        });
    });
}

function dbAll(sql, params = []) {
    const db = openImageDb();
    return new Promise((resolve, reject) => {
        db.all(sql, params, (err, rows) => {
            if (err) return reject(err);
            resolve(rows);
        });
    });
}

async function initImageDb() {
    await dbRun(`
        CREATE TABLE IF NOT EXISTS user_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            side TEXT NOT NULL,
            lane TEXT,
            file_name TEXT NOT NULL,
            absolute_path TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            modified_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, absolute_path)
        );
    `);
    await dbRun(`
        CREATE INDEX IF NOT EXISTS idx_user_images_username_modified
        ON user_images(username, modified_at DESC);
    `);
}

async function syncUserImagesToDb(username, entries) {
    const safeUsername = String(username || '').trim();
    if (!safeUsername) return;
    const safeEntries = Array.isArray(entries) ? entries : [];
    await dbRun('BEGIN TRANSACTION');
    try {
        for (const img of safeEntries) {
            await dbRun(
                `
                INSERT INTO user_images (username, side, lane, file_name, absolute_path, size, modified_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(username, absolute_path) DO UPDATE SET
                    side = excluded.side,
                    lane = excluded.lane,
                    file_name = excluded.file_name,
                    size = excluded.size,
                    modified_at = excluded.modified_at,
                    updated_at = CURRENT_TIMESTAMP
                `,
                [
                    safeUsername,
                    String(img.side || ''),
                    String(img.lane || ''),
                    String(img.fileName || ''),
                    String(img.absolutePath || ''),
                    Number(img.size || 0),
                    img.modifiedAt ? new Date(img.modifiedAt).toISOString() : null,
                ]
            );
        }

        if (safeEntries.length > 0) {
            const paths = safeEntries.map((img) => String(img.absolutePath || ''));
            const placeholders = paths.map(() => '?').join(',');
            await dbRun(
                `DELETE FROM user_images WHERE username = ? AND absolute_path NOT IN (${placeholders})`,
                [safeUsername, ...paths]
            );
        } else {
            await dbRun('DELETE FROM user_images WHERE username = ?', [safeUsername]);
        }
        await dbRun('COMMIT');
    } catch (err) {
        await dbRun('ROLLBACK');
        throw err;
    }
}

async function getStoredMergeImageEntries(username) {
    const safeUsername = String(username || '').trim();
    if (!safeUsername) return [];
    const rows = await dbAll(
        `
        SELECT side, lane, file_name, size, modified_at, absolute_path
        FROM user_images
        WHERE username = ?
        ORDER BY datetime(modified_at) DESC, id DESC
        `,
        [safeUsername]
    );
    return rows.map((row) => ({
        side: row.side,
        lane: row.lane,
        fileName: row.file_name,
        size: Number(row.size || 0),
        modifiedAt: row.modified_at ? new Date(row.modified_at) : null,
        absolutePath: row.absolute_path,
    }));
}

// Helper to get user-specific directories
function getUserDirs(username) {
    const userDir = path.join(DATA_DIR, 'users', username);
    const uploadsDir = path.join(userDir, 'uploads');
    const pipelineDir = path.join(userDir, 'pipeline');
    const dataFile = path.join(userDir, 'drawn_data.json');

    if (!fs.existsSync(userDir)) fs.mkdirSync(userDir, { recursive: true });
    if (!fs.existsSync(uploadsDir)) fs.mkdirSync(uploadsDir, { recursive: true });
    if (!fs.existsSync(pipelineDir)) fs.mkdirSync(pipelineDir, { recursive: true });
    if (!fs.existsSync(dataFile)) fs.writeFileSync(dataFile, JSON.stringify([]));

    // Ensure pipeline subdirs exist for this user
    PIPELINE_SUBDIRS.forEach(sub => {
        const subPath = path.join(pipelineDir, sub);
        if (!fs.existsSync(subPath)) fs.mkdirSync(subPath, { recursive: true });
    });

    return { userDir, uploadsDir, pipelineDir, dataFile };
}

// Subdirectories that should always exist in pipeline
const PIPELINE_SUBDIRS = ['LHS_KMLs', 'RHS_KMLs', 'Excels', 'Merge_KMLs'];

// Ensure base directories exist
function ensureDirectories() {
    const usersBaseDir = path.join(DATA_DIR, 'users');
    try {
        // Railway/container startup can race with volume mount readiness.
        // Use recursive creation and explicit logs so path issues are visible in deploy logs.
        fs.mkdirSync(DATA_DIR, { recursive: true });
        fs.mkdirSync(usersBaseDir, { recursive: true });
        if (!fs.existsSync(USERS_FILE)) fs.writeFileSync(USERS_FILE, JSON.stringify([]));
        console.log(`[BOOT] DATA_DIR ready: ${DATA_DIR}`);
    } catch (error) {
        console.error(`[BOOT] Failed to initialize DATA_DIR "${DATA_DIR}":`, error);
        throw error;
    }
}

ensureDirectories();
initImageDb().catch((error) => {
    console.error('[DB] SQLite init failed:', error);
});

app.use(cors({
    origin: '*',
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    // Must include custom headers or browsers block cross-origin requests (save/pipeline from hosted frontend)
    allowedHeaders: ['Content-Type', 'Authorization', 'x-username'],
}));

app.use(bodyParser.json({ limit: '50mb' }));
app.use(express.urlencoded({ limit: '50mb', extended: true }));

// Health check route for Railway
app.get('/', (req, res) => {
    res.json({ status: 'Backend is running successfully', timestamp: new Date() });
});

// --- Authentication Routes ---

app.post('/api/register', async (req, res) => {
    try {
        const { username, password } = req.body;
        if (!username || !password) {
            return res.status(400).json({ success: false, message: 'Username and password are required' });
        }
        if (username === HARDCODED_TEST_USER) {
            return res.status(400).json({ success: false, message: 'Username is reserved' });
        }

        const users = JSON.parse(fs.readFileSync(USERS_FILE, 'utf8'));
        if (users.find(u => u.username === username)) {
            return res.status(400).json({ success: false, message: 'Username already exists' });
        }

        const hashedPassword = await bcrypt.hash(password, 10);
        users.push({ username, password: hashedPassword });
        fs.writeFileSync(USERS_FILE, JSON.stringify(users, null, 2));

        res.json({ success: true, message: 'User registered successfully' });
    } catch (error) {
        console.error('Registration error:', error);
        res.status(500).json({ success: false, message: 'Registration failed' });
    }
});

function verifyUserCredentials(username, password) {
    const u = String(username || '').trim();
    if (!u || password === undefined || password === null) return false;
    const p = String(password);
    if (u === HARDCODED_TEST_USER && p === HARDCODED_TEST_PASSWORD) return true;
    try {
        const users = JSON.parse(fs.readFileSync(USERS_FILE, 'utf8'));
        const user = users.find((x) => x.username === u);
        if (!user) return false;
        return bcrypt.compareSync(p, user.password);
    } catch {
        return false;
    }
}

app.post('/api/login', (req, res) => {
    try {
        const { username, password } = req.body;
        const u = String(username || '').trim();
        const ok = verifyUserCredentials(u, password);
        if (!ok) {
            return res.status(401).json({ success: false, message: 'Invalid username or password' });
        }
        const tokenUsername = u === HARDCODED_TEST_USER ? HARDCODED_TEST_USER : u;
        const token = jwt.sign({ username: tokenUsername }, JWT_SECRET);
        res.json({ success: true, token, username: tokenUsername });
    } catch (error) {
        console.error('Login error:', error);
        res.status(500).json({ success: false, message: 'Login failed' });
    }
});

/**
 * POST /api/login/images
 * Body JSON (only these two fields): { "username", "password" }
 * On success: validates login, returns JWT + every pipeline image file for that user
 * (LHS_images, RHS_images, LHS_kml_merge_images, RHS_kml_merge_images) as base64 in "images".
 * Size cap: env MAX_LOGIN_IMAGES_JSON_BYTES (default 200 MB raw total before base64); raise if needed.
 */
app.post('/api/login/images', async (req, res) => {
    try {
        const { username, password } = req.body || {};
        const u = String(username || '').trim();
        if (!u || password === undefined || password === null) {
            return res.status(400).json({
                success: false,
                message: 'Send JSON body with only "username" and "password".',
            });
        }
        if (!verifyUserCredentials(u, password)) {
            return res.status(401).json({ success: false, message: 'Invalid username or password' });
        }

        const tokenUsername = u === HARDCODED_TEST_USER ? HARDCODED_TEST_USER : u;
        const token = jwt.sign({ username: tokenUsername }, JWT_SECRET);

        const raw = getMergeImageEntries(tokenUsername);
        let sourceEntries = raw;
        try {
            await syncUserImagesToDb(tokenUsername, raw);
            const stored = await getStoredMergeImageEntries(tokenUsername);
            if (stored.length > 0) sourceEntries = stored;
        } catch (dbError) {
            console.error('SQLite sync/read failed, using filesystem fallback:', dbError);
        }
        const baseUrl = getRequestBaseUrl(req);

        // For /api/login/images return only lane/per-image outputs and exclude merge-strip images.
        const filteredEntries = sourceEntries.filter((img) => !isMergeKmlStripPath(img.absolutePath));

        const images = filteredEntries.map((img) => {
            const access = createPublicImageAccessToken(tokenUsername, img.absolutePath);
            const encodedPath = encodeURIComponent(img.absolutePath);
            const encodedAccess = encodeURIComponent(access);
            return {
                side: img.side,
                lane: img.lane,
                fileName: img.fileName,
                size: img.size,
                modifiedAt: img.modifiedAt,
                url: `/api/public-image?path=${encodedPath}&access=${encodedAccess}`,
                publicUrl: `${baseUrl}/api/public-image?path=${encodedPath}&access=${encodedAccess}`
            };
        });

        return res.json({
            success: true,
            username: tokenUsername,
            token,
            count: images.length,
            images,
        });
    } catch (error) {
        console.error('Login/images error:', error);
        return res.status(500).json({ success: false, message: 'Login/images failed' });
    }
});

/**
 * Resolve the caller from Authorization: Bearer <JWT> or Basic base64(user:pass).
 * Returns the authenticated username, or null after sending an error response.
 */
function resolveAuthenticatedUser(req, res) {
    const auth = (req.headers.authorization || '').trim();
    const realm = 'Basic realm="KML Merge Images"';

    if (auth.startsWith('Bearer ')) {
        const token = auth.slice(7).trim();
        if (!token) {
            res.status(401).set('WWW-Authenticate', realm).json({
                success: false,
                message: 'Authentication required.',
            });
            return null;
        }
        try {
            const payload = jwt.verify(token, JWT_SECRET);
            const u = payload && payload.username ? String(payload.username).trim() : '';
            if (!u) {
                res.status(401).json({ success: false, message: 'Invalid token payload' });
                return null;
            }
            return u;
        } catch {
            res.status(401).json({ success: false, message: 'Invalid or expired token' });
            return null;
        }
    }

    if (auth.startsWith('Basic ')) {
        let u = '';
        let p = '';
        try {
            const decoded = Buffer.from(auth.slice(6), 'base64').toString('utf8');
            const idx = decoded.indexOf(':');
            u = idx >= 0 ? decoded.slice(0, idx).trim() : decoded.trim();
            p = idx >= 0 ? decoded.slice(idx + 1) : '';
        } catch {
            res.status(401).set('WWW-Authenticate', realm).json({
                success: false,
                message: 'Invalid Basic authorization header.',
            });
            return null;
        }
        const ok = verifyUserCredentials(u, p);
        if (!ok) {
            res.status(401).set('WWW-Authenticate', realm).json({
                success: false,
                message: 'Invalid username or password',
            });
            return null;
        }
        return u;
    }

    res.status(401)
        .set('WWW-Authenticate', realm)
        .json({
            success: false,
            message:
                'Authentication required. Send Authorization: Bearer <token> from POST /api/login, or Basic (username:password).',
        });
    return null;
}

/** Only the user named in the URL may access that user's merge-image routes. */
function requireMergeImagesAccess(req, res, next) {
    try {
        const routeUsername = (req.params.username || '').trim();
        if (!routeUsername) {
            return res.status(400).json({ success: false, message: 'Username is required' });
        }
        const authedUsername = resolveAuthenticatedUser(req, res);
        if (!authedUsername) return;
        if (authedUsername !== routeUsername) {
            return res.status(403).json({
                success: false,
                message: 'You can only access merge images for the account you authenticated as.',
            });
        }
        req.user = { username: authedUsername };
        next();
    } catch (err) {
        console.error('requireMergeImagesAccess:', err);
        if (!res.headersSent) {
            res.status(500).json({ success: false, message: 'Authentication error' });
        }
    }
}

/** Short-lived URL token so /api/public-image can be opened directly in browser. */
function createPublicImageAccessToken(username, absolutePath) {
    return jwt.sign(
        {
            type: 'public_image_access',
            username,
            path: String(absolutePath || '')
        },
        JWT_SECRET,
        { expiresIn: '12h' }
    );
}

/** JWT from Authorization: Bearer or ?token= (GET / img / window.open). */
function extractBearerToken(req) {
    const authHeader = req.headers && req.headers.authorization;
    if (authHeader && typeof authHeader === 'string' && authHeader.startsWith('Bearer ')) {
        const t = authHeader.slice(7).trim();
        if (t) return t;
    }
    const q = req.query && req.query.token;
    if (q && typeof q === 'string' && q.trim()) return q.trim();
    return null;
}
const authenticateToken = (req, res, next) => {
    const token = extractBearerToken(req);
    if (!token) {
        return res.status(401).json({
            success: false,
            message: 'Authentication required. Use Authorization: Bearer <token> or ?token= for GET requests.'
        });
    }
    try {
        const decoded = jwt.verify(token, JWT_SECRET);
        if (!decoded || typeof decoded.username !== 'string' || !decoded.username.trim()) {
            return res.status(401).json({ success: false, message: 'Invalid token payload' });
        }
        req.user = { username: decoded.username.trim() };
        next();
    } catch (err) {
        return res.status(401).json({ success: false, message: 'Invalid or expired token' });
    }
};

function assertRouteUsername(req, res, usernameParam) {
    const u = (usernameParam || '').trim();
    if (!u) {
        res.status(400).json({ success: false, message: 'Username is required' });
        return false;
    }
    if (u !== req.user.username) {
        res.status(403).json({ success: false, message: 'Username does not match authenticated user' });
        return false;
    }
    return true;
}


// Helper function to convert GeoJSON to KML
function geojsonToKml(features, name) {
    let kml = `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>${name}</name>
    <Style id="defaultStyle">
      <PolyStyle>
        <colorMode>normal</colorMode>
        <fill>0</fill>
        <outline>1</outline>
      </PolyStyle>
    </Style>`;

    features.forEach((feature, index) => {
        const geom = feature.geometry;
        const props = feature.properties || {};
        const featName = props.name || `Feature ${index + 1}`;

        kml += `
    <Placemark>
      <name>${featName}</name>
      <styleUrl>#defaultStyle</styleUrl>`;

        if (geom.type === 'Point') {
            kml += `
      <Point>
        <coordinates>${geom.coordinates[0]},${geom.coordinates[1]},0</coordinates>
      </Point>`;
        } else if (geom.type === 'LineString') {
            const coords = geom.coordinates.map(c => `${c[0]},${c[1]},0`).join(' ');
            kml += `
      <LineString>
        <tessellate>1</tessellate>
        <coordinates>${coords}</coordinates>
      </LineString>`;
        } else if (geom.type === 'Polygon') {
            const outerCoords = geom.coordinates[0].map(c => `${c[0]},${c[1]},0`).join(' ');
            kml += `
      <Polygon>
        <tessellate>1</tessellate>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>${outerCoords}</coordinates>
          </LinearRing>
        </outerBoundaryIs>`;

            if (geom.coordinates.length > 1) {
                for (let i = 1; i < geom.coordinates.length; i++) {
                    const innerCoords = geom.coordinates[i].map(c => `${c[0]},${c[1]},0`).join(' ');
                    kml += `
        <innerBoundaryIs>
          <LinearRing>
            <coordinates>${innerCoords}</coordinates>
          </LinearRing>
        </innerBoundaryIs>`;
                }
            }
            kml += `
      </Polygon>`;
        }

        kml += `
    </Placemark>`;
    });

    kml += `
  </Document>
</kml>`;
    return kml;
}

// Helper function to process data with Python script
async function processWithPython(metadata, kmlContent, userDirs) {
    const kmlCreationDir = path.join(userDirs.userDir, 'kml_creation');
    const inputKmlPath = path.join(kmlCreationDir, 'input.kml');
    const pythonScriptPath = path.join(__dirname, 'kml_creation', 'KML_creation.py');
    const logPath = path.join(userDirs.userDir, 'python_output_log.txt');
    const errLogPath = path.join(userDirs.userDir, 'python_error_log.txt');

    return new Promise(async (resolve, reject) => {
        try {
            // 1. Prepare environment
            if (!fs.existsSync(kmlCreationDir)) fs.mkdirSync(kmlCreationDir, { recursive: true });
            fs.writeFileSync(inputKmlPath, kmlContent);

            // 2. Resolve Python path
            // Container / Railway venv first
            const venvPath = '/opt/venv/bin/python';
            let pythonExe = null;

            if (fs.existsSync(venvPath)) {
                pythonExe = venvPath;
            } else {
                // On Windows the `python3` command is often the Microsoft Store App Execution Alias,
                // which exits 0 without running anything (just prints an install hint to stderr).
                // So we must verify the candidate's stdout actually looks like Python, not just trust
                // the exit code. Try candidates in order of likelihood per platform.
                const candidates = process.platform === 'win32'
                    ? ['python', 'py', 'python3']
                    : ['python3', 'python', 'py'];

                for (const candidate of candidates) {
                    try {
                        const { stdout, stderr } = await execPromise(`${candidate} --version`);
                        const combined = `${stdout || ''} ${stderr || ''}`;
                        if (/^\s*Python\s+\d+\.\d+/i.test(combined)) {
                            pythonExe = candidate;
                            break;
                        }
                    } catch (_) {
                        // try next candidate
                    }
                }

                if (!pythonExe) {
                    return reject(new Error(
                        'No working Python interpreter found. Install Python and ensure `python` (or `python3`) is on PATH.'
                    ));
                }
            }

            // 3. Prepare Arguments (Exactly 8 parameters as required)
            const args = [
                pythonScriptPath,
                inputKmlPath,
                userDirs.pipelineDir,
                (parseFloat(metadata.chainage) || 0).toString(),
                "5", // interval
                (parseInt(metadata.laneCount) || 4).toString(),
                (parseFloat(metadata.kmlMergeOffset) || 0.100).toString(),
                "3.4", // laneStep
                (parseFloat(metadata.offsetType) || 2.75).toString()
            ];

            console.log(`[PYTHON] [USER:${path.basename(userDirs.userDir)}] Executing: ${pythonExe} ${args.join(' ')}`);

            // 4. Spawn process
            const { spawn } = require('child_process');
            const childEnv = {
                ...process.env,
                SATELLITE_DATE_START: metadata.startDate || process.env.SATELLITE_DATE_START || '2026-02-10',
                SATELLITE_DATE_END: metadata.endDate || process.env.SATELLITE_DATE_END || '2026-02-20',
                IMAGE_DIRECTION: metadata.imageDirection || process.env.IMAGE_DIRECTION || 'down_to_up',
            };
            const child = spawn(pythonExe, args, { env: childEnv });

            let stdoutData = '';
            let stderrData = '';

            child.stdout.on('data', (data) => {
                const str = data.toString();
                stdoutData += str;
                process.stdout.write(`[PYTHON STDOUT] ${str}`);
            });

            child.stderr.on('data', (data) => {
                const str = data.toString();
                stderrData += str;
                process.stderr.write(`[PYTHON STDERR] ${str}`);
            });

            child.on('close', (code) => {
                // Write logs
                const fullLog = `COMMAND: ${pythonExe} ${args.join(' ')}\n\nSTDOUT:\n${stdoutData}\n\nSTDERR:\n${stderrData}`;
                fs.writeFileSync(logPath, fullLog);

                if (code !== 0) {
                    const errorMsg = stderrData || stdoutData || 'Unknown error';
                    fs.writeFileSync(errLogPath, `EXIT CODE ${code}\n\n${errorMsg}`);

                    // Check for our custom error markers
                    const errorMatch = errorMsg.match(/CRITICAL_PYTHON_ERROR_START([\s\S]*)CRITICAL_PYTHON_ERROR_END/);
                    const specificError = errorMatch ? errorMatch[1].trim() : errorMsg;

                    return reject(new Error(`Python script failed (Code ${code}): ${specificError}`));
                }

                // 5. Verification: Check if folders actually contain files
                const excelsDir = path.join(userDirs.pipelineDir, 'Excels');
                const mergeDir = path.join(userDirs.pipelineDir, 'Merge_KMLs');

                const hasExcels = fs.existsSync(excelsDir) && fs.readdirSync(excelsDir).length > 0;
                const hasKmls = fs.existsSync(mergeDir) && fs.readdirSync(mergeDir).length > 0;

                if (!hasExcels && !hasKmls) {
                    return reject(new Error('Python script finished but NO files were generated. Check input KML coordinates.'));
                }

                console.log('[PYTHON] Execution successful. Files generated.');
                resolve(true);
            });

            child.on('error', (err) => {
                reject(new Error(`Failed to start Python process: ${err.message}`));
            });

        } catch (error) {
            reject(error);
        }
    });
}

// Helper function to save data to the pipeline folder
async function saveToPipeline(metadata, content, userDirs, isKmlContent = false) {
    let kmlContent = isKmlContent ? content : geojsonToKml(content, 'Drawn_Data');
    await processWithPython(metadata, kmlContent, userDirs);
    return 'Merge_KMLs';
}

function getRequestBaseUrl(req) {
    const configuredBaseUrl = String(process.env.PUBLIC_BASE_URL || '').trim();
    if (configuredBaseUrl) {
        return configuredBaseUrl.replace(/\/+$/, '');
    }
    const protocol = (req.headers['x-forwarded-proto'] || req.protocol || 'http').split(',')[0].trim();
    const host = req.get('host');
    const localHostRegex = /^(localhost|127\.0\.0\.1|::1)$/i;
    const splitHost = (hostValue) => {
        if (!hostValue) return { hostname: '', port: '' };
        if (hostValue.startsWith('[')) {
            const idx = hostValue.indexOf(']');
            if (idx >= 0) {
                const hostname = hostValue.slice(1, idx);
                const port = hostValue.slice(idx + 1).replace(/^:/, '');
                return { hostname, port };
            }
        }
        const [hostname, port] = hostValue.split(':');
        return { hostname: hostname || '', port: port || '' };
    };
    const detectLanIp = () => {
        const nets = os.networkInterfaces();
        for (const key of Object.keys(nets)) {
            for (const net of nets[key] || []) {
                if (
                    net &&
                    net.family === 'IPv4' &&
                    !net.internal &&
                    net.address &&
                    net.address !== '127.0.0.1'
                ) {
                    return net.address;
                }
            }
        }
        return '';
    };

    const { hostname, port } = splitHost(host || '');
    if (localHostRegex.test(hostname)) {
        const preferredIp = String(process.env.PUBLIC_IP || process.env.PUBLIC_HOST_IP || '').trim() || detectLanIp();
        if (preferredIp) {
            return `http://${preferredIp}${port ? `:${port}` : ''}`;
        }
    }
    return `${protocol}://${host}`;
}

function publicImageLinks(req, absolutePath) {
    const baseUrl = getRequestBaseUrl(req);
    const enc = encodeURIComponent(absolutePath);
    const ts = extractBearerToken(req);
    const suff = ts ? `&token=${encodeURIComponent(ts)}` : '';
    return {
        url: `/api/public-image?path=${enc}${suff}`,
        publicUrl: `${baseUrl}/api/public-image?path=${enc}${suff}`
    };
}

function getMergeImageEntries(username) {
    const userDirs = getUserDirs(username);
    const imageExtRegex = /\.(png|jpe?g|gif|webp|bmp)$/i;
    const images = [];
    const addImageEntry = (absolutePath, side, lane) => {
        const stats = fs.statSync(absolutePath);
        images.push({
            side,
            lane,
            fileName: path.basename(absolutePath),
            size: stats.size,
            modifiedAt: stats.mtime,
            absolutePath
        });
    };

    const addLaneImages = (rootDir, side) => {
        if (!fs.existsSync(rootDir) || !fs.statSync(rootDir).isDirectory()) return;
        const entries = fs.readdirSync(rootDir, { withFileTypes: true });
        entries.forEach((entry) => {
            if (!entry.isDirectory()) return;
            const laneName = entry.name;
            const laneDir = path.join(rootDir, laneName);
            fs.readdirSync(laneDir)
                .filter((fileName) => imageExtRegex.test(fileName))
                .forEach((fileName) => addImageEntry(path.join(laneDir, fileName), side, laneName));
        });
    };

    const addFlatImages = (rootDir, side, laneLabel) => {
        if (!fs.existsSync(rootDir) || !fs.statSync(rootDir).isDirectory()) return;
        fs.readdirSync(rootDir, { withFileTypes: true })
            .filter((entry) => entry.isFile() && imageExtRegex.test(entry.name))
            .forEach((entry) => addImageEntry(path.join(rootDir, entry.name), side, laneLabel));
    };

    addLaneImages(path.join(userDirs.pipelineDir, 'LHS_KMLs', 'LHS_images'), 'lhs');
    addLaneImages(path.join(userDirs.pipelineDir, 'RHS_KMLs', 'RHS_images'), 'rhs');
    addFlatImages(path.join(userDirs.pipelineDir, 'LHS_KMLs', 'LHS_kml_merge_images'), 'lhs', 'MERGED');
    addFlatImages(path.join(userDirs.pipelineDir, 'RHS_KMLs', 'RHS_kml_merge_images'), 'rhs', 'MERGED');

    images.sort((a, b) => new Date(b.modifiedAt) - new Date(a.modifiedAt));
    return images;
}

function collectImageFilesFromPath(targetPath) {
    const imageExtRegex = /\.(png|jpe?g|gif|webp|bmp)$/i;
    const out = [];
    const walk = (dir) => {
        let entries = [];
        try {
            entries = fs.readdirSync(dir, { withFileTypes: true });
        } catch {
            return;
        }
        for (const entry of entries) {
            const abs = path.join(dir, entry.name);
            if (entry.isDirectory()) {
                walk(abs);
            } else if (entry.isFile() && imageExtRegex.test(entry.name)) {
                out.push(abs);
            }
        }
    };
    walk(targetPath);
    return out;
}

function mimeTypeForFile(filePath) {
    const ext = path.extname(String(filePath || '')).toLowerCase();
    if (ext === '.png') return 'image/png';
    if (ext === '.jpg' || ext === '.jpeg') return 'image/jpeg';
    if (ext === '.gif') return 'image/gif';
    if (ext === '.webp') return 'image/webp';
    if (ext === '.bmp') return 'image/bmp';
    return 'application/octet-stream';
}

function getDistressImageSources(username, pipelineSubPath = '') {
    const userDirs = getUserDirs(username);
    const trimmedPath = String(pipelineSubPath || '').trim();

    // Default: use merge-strip images generated from KML pipeline.
    if (!trimmedPath) {
        const mergeEntries = getMergeImageEntries(username).filter((img) =>
            isMergeKmlStripPath(img.absolutePath)
        );
        return mergeEntries.map((img) => ({
            absolutePath: img.absolutePath,
            displayName: img.fileName,
        }));
    }

    const resolvedPath = path.resolve(userDirs.pipelineDir, trimmedPath);
    if (!resolvedPath.startsWith(userDirs.pipelineDir)) {
        throw new Error('Invalid pipeline path');
    }
    if (!fs.existsSync(resolvedPath) || !fs.statSync(resolvedPath).isDirectory()) {
        throw new Error('Pipeline folder not found');
    }
    if (!/(^|[/\\])(LHS|RHS)_kml_merge_images$/i.test(resolvedPath.replace(/\\/g, '/'))) {
        throw new Error('Only *_kml_merge_images folders are allowed');
    }

    const files = collectImageFilesFromPath(resolvedPath);
    return files.map((absolutePath) => ({
        absolutePath,
        displayName: path.basename(absolutePath),
    }));
}

/** True only for flat strip PNGs in *_kml_merge_images (excludes LHS_images/L1 etc.). */
function isMergeKmlStripPath(absolutePath) {
    return /[/\\](LHS|RHS)_kml_merge_images[/\\]/i.test(absolutePath || '');
}

function clearUserWorkingData(userDirs, username, options = {}) {
    const { preserveFiles = [] } = options;
    const preserveSet = new Set(
        preserveFiles
            .filter(Boolean)
            .map((filePath) => path.resolve(filePath))
    );

    if (fs.existsSync(userDirs.dataFile)) {
        try {
            fs.writeFileSync(userDirs.dataFile, JSON.stringify([], null, 2));
        } catch (e) {
            console.error('Error clearing data file:', e);
        }
    }

    if (fs.existsSync(userDirs.uploadsDir)) {
        try {
            const uploadFiles = fs.readdirSync(userDirs.uploadsDir);
            for (const file of uploadFiles) {
                const filePath = path.join(userDirs.uploadsDir, file);
                try {
                    if (fs.existsSync(filePath) && fs.statSync(filePath).isFile() && !preserveSet.has(path.resolve(filePath))) {
                        fs.unlinkSync(filePath);
                    }
                } catch (err) {
                    console.error(`Error deleting upload file ${file}:`, err);
                }
            }
        } catch (e) {
            console.error('Error reading uploads dir:', e);
        }
    }

    const subDirs = ['LHS_KMLs', 'RHS_KMLs', 'Excels', 'Merge_KMLs'];
    for (const sub of subDirs) {
        const subPath = path.join(userDirs.pipelineDir, sub);
        if (fs.existsSync(subPath)) {
            try {
                const items = fs.readdirSync(subPath);
                for (const item of items) {
                    const itemPath = path.join(subPath, item);
                    try {
                        if (fs.existsSync(itemPath)) {
                            if (fs.statSync(itemPath).isDirectory()) {
                                fs.rmSync(itemPath, { recursive: true, force: true });
                            } else {
                                fs.unlinkSync(itemPath);
                            }
                        }
                    } catch (err) {
                        console.error(`Error deleting item ${item} in ${sub}:`, err);
                    }
                }
            } catch (err) {
                console.error(`Error reading directory ${sub}:`, err);
            }
        }
    }

    if (fs.existsSync(userDirs.pipelineDir)) {
        try {
            const rootItems = fs.readdirSync(userDirs.pipelineDir);
            for (const item of rootItems) {
                const itemPath = path.join(userDirs.pipelineDir, item);
                try {
                    if (fs.existsSync(itemPath) && fs.statSync(itemPath).isFile()) {
                        fs.unlinkSync(itemPath);
                    }
                } catch (err) {
                    console.error(`Error deleting root file ${item}:`, err);
                }
            }
        } catch (err) {
            console.error('Error reading pipeline root:', err);
        }
    }

    console.log(`User data cleared for: ${username}`);
}

// WATCHER REMOVED to prevent race conditions during save operations.
// Pipeline is now explicitly called in /save and /upload-kml routes.

// Routes
app.get('/api/merge-images/:username', requireMergeImagesAccess, (req, res) => {
    try {
        const username = (req.params.username || '').trim();
        if (!username) {
            return res.status(400).json({ success: false, message: 'Username is required' });
        }
        const onlyRaw = String(req.query.only || req.query.merge_only || '').toLowerCase();
        // merge_kml / merge = only flat strips under *_kml_merge_images (not LHS_images/L1 per-lane PNGs).
        // all / lanes / (empty) = everything getMergeImageEntries finds (merge strips + per-lane images).
        const onlyMergeKml = ['1', 'true', 'yes', 'merge_kml', 'merge'].includes(onlyRaw);
        const onlyLanes = ['lanes', 'lane', 'per_lane', 'lhs_images', 'rhs_images'].includes(onlyRaw);
        let raw = getMergeImageEntries(username);
        const unfilteredCount = raw.length;
        if (onlyMergeKml) {
            raw = raw.filter((img) => isMergeKmlStripPath(img.absolutePath));
        } else if (onlyLanes) {
            raw = raw.filter((img) => !isMergeKmlStripPath(img.absolutePath));
        }
        const images = raw.map((img) => {
            const links = publicImageLinks(req, img.absolutePath);
            return {
                side: img.side,
                lane: img.lane,
                fileName: img.fileName,
                size: img.size,
                modifiedAt: img.modifiedAt,
                url: links.url,
                publicUrl: links.publicUrl
            };
        });

        let hint;
        if (images.length === 0 && onlyMergeKml) {
            hint =
                'No files in *_kml_merge_images for this user. Omit ?only=merge_kml (or use ?only=lanes) to list per-lane images under LHS_images/RHS_images, or run Save so the pipeline generates merge strips.';
        } else if (images.length === 0 && onlyLanes) {
            hint =
                'No per-lane images found. Use ?only=merge_kml for merged strip PNGs only, or omit ?only to list all pipeline images.';
        } else if (images.length === 0) {
            hint =
                'No image files found under this user pipeline. Save KML data once so LHS_images / RHS_images / *_kml_merge_images are populated.';
        } else if (onlyMergeKml) {
            hint =
                'Listing only merged strip PNGs (*_kml_merge_images). Per-lane files (LHS_images/...) are excluded; omit ?only=merge_kml to include them.';
        }

        return res.json({
            success: true,
            username,
            count: images.length,
            onlyMergeKml: !!onlyMergeKml,
            onlyLanes: !!onlyLanes,
            scannedBeforeFilter: unfilteredCount,
            ...(hint ? { hint } : {}),
            images
        });
    } catch (error) {
        console.error('Error fetching merge images:', error);
        return res.status(500).json({ success: false, message: 'Error fetching merge images' });
    }
});

app.get('/api/merge-images/:username/:side/:fileName', requireMergeImagesAccess, (req, res) => {
    try {
        if (!assertRouteUsername(req, res, req.params.username)) return;
        const { side } = req.params;
        const username = req.user.username;
        const decodedFileName = decodeURIComponent(req.params.fileName || '');

        const userDirs = getUserDirs(username);

        const sideFolderMap = {
            lhs: path.join(userDirs.pipelineDir, 'LHS_KMLs', 'LHS_kml_merge_images'),
            rhs: path.join(userDirs.pipelineDir, 'RHS_KMLs', 'RHS_kml_merge_images')
        };

        const targetFolder = sideFolderMap[side];
        if (!targetFolder) {
            return res.status(400).json({ success: false, message: 'Invalid side. Use lhs or rhs.' });
        }

        const fullPath = path.resolve(targetFolder, decodedFileName);
        if (!fullPath.startsWith(path.resolve(targetFolder) + path.sep)) {
            return res.status(403).json({ success: false, message: 'Access denied' });
        }

        if (!fs.existsSync(fullPath) || !fs.statSync(fullPath).isFile()) {
            return res.status(404).json({ success: false, message: 'Image not found' });
        }

        return res.sendFile(fullPath);
    } catch (error) {
        console.error('Error serving merge image:', error);
        return res.status(500).json({ success: false, message: 'Error serving merge image' });
    }
});

/**
 * All PNGs (etc.) from *_kml_merge_images in one download (ZIP).
 * side: lhs | rhs | both — maps to pipeline/LHS_KMLs/LHS_kml_merge_images and RHS_KMLs/RHS_kml_merge_images
 */
function collectMergeKmlFolderFiles(username, side, options = {}) {
    const flat = !!options.flat;
    const userDirs = getUserDirs(username);
    const imageExtRegex = /\.(png|jpe?g|gif|webp|bmp)$/i;
    const out = [];
    const usedNames = new Set();
    const s = String(side || 'rhs').toLowerCase();
    const isBoth = s === 'both';

    const addFlat = (absFolder, zipDirName, sidePrefix) => {
        if (!fs.existsSync(absFolder) || !fs.statSync(absFolder).isDirectory()) return;
        fs.readdirSync(absFolder).forEach((name) => {
            const fp = path.join(absFolder, name);
            if (!fs.statSync(fp).isFile() || !imageExtRegex.test(name)) return;
            let nameInZip;
            if (!flat) {
                nameInZip = `${zipDirName}/${name}`;
            } else if (isBoth) {
                nameInZip = `${sidePrefix}_${name}`;
            } else {
                nameInZip = name;
            }
            if (flat) {
                let tryName = nameInZip;
                let n = 2;
                while (usedNames.has(tryName)) {
                    const ext = path.extname(name);
                    const base = name.slice(0, -ext.length);
                    tryName = `${base}_${n}${ext}`;
                    n += 1;
                }
                usedNames.add(tryName);
                nameInZip = tryName;
            }
            out.push({ abs: fp, nameInZip });
        });
    };

    if (s === 'lhs' || isBoth) {
        addFlat(
            path.join(userDirs.pipelineDir, 'LHS_KMLs', 'LHS_kml_merge_images'),
            'LHS_kml_merge_images',
            'LHS'
        );
    }
    if (s === 'rhs' || isBoth) {
        addFlat(
            path.join(userDirs.pipelineDir, 'RHS_KMLs', 'RHS_kml_merge_images'),
            'RHS_kml_merge_images',
            'RHS'
        );
    }
    return out;
}

app.get('/api/merge-kml-images-zip/:username/:side', requireMergeImagesAccess, (req, res) => {
    try {
        if (!assertRouteUsername(req, res, req.params.username)) return;
        const username = req.user.username;
        const sideRaw = (req.params.side || 'rhs').trim();
        const side = sideRaw.toLowerCase();
        if (!['lhs', 'rhs', 'both'].includes(side)) {
            return res.status(400).json({
                success: false,
                message: 'Invalid side. Use lhs, rhs, or both (folder merge_kml_images).'
            });
        }

        const flat = ['1', 'true', 'yes'].includes(String(req.query.flat || '').toLowerCase());
        const files = collectMergeKmlFolderFiles(username, side, { flat });
        if (files.length === 0) {
            return res.status(404).json({
                success: false,
                message: 'No images found in merge_kml_images folder(s) for this user.'
            });
        }

        const safeName = `merge_kml_images_${username}_${side}${flat ? '_flat' : ''}.zip`.replace(/[^\w.\-]+/g, '_');
        res.setHeader('Content-Type', 'application/zip');
        res.setHeader('Content-Disposition', `attachment; filename="${safeName}"`);

        const archive = archiver('zip', { zlib: { level: 9 } });
        archive.on('error', (err) => {
            console.error('merge-kml-images-zip archive error:', err);
            if (!res.headersSent) {
                res.status(500).json({ success: false, message: 'Error creating zip' });
            }
        });
        archive.pipe(res);
        files.forEach(({ abs, nameInZip }) => {
            archive.file(abs, { name: nameInZip });
        });
        archive.finalize();
    } catch (error) {
        console.error('Error in merge-kml-images-zip:', error);
        if (!res.headersSent) {
            res.status(500).json({ success: false, message: 'Error building merge images archive' });
        }
    }
});

function guessImageMime(fileName) {
    const ext = path.extname(fileName || '').toLowerCase();
    if (ext === '.png') return 'image/png';
    if (ext === '.jpg' || ext === '.jpeg') return 'image/jpeg';
    if (ext === '.gif') return 'image/gif';
    if (ext === '.webp') return 'image/webp';
    if (ext === '.bmp') return 'image/bmp';
    return 'application/octet-stream';
}

/** Flat list of files only in *_kml_merge_images (no lane subfolders). */
function listMergeKmlStripFiles(username, side) {
    const userDirs = getUserDirs(username);
    const imageExtRegex = /\.(png|jpe?g|gif|webp|bmp)$/i;
    const out = [];
    const s = String(side || 'rhs').toLowerCase();
    const scan = (folder, sideId) => {
        if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) return;
        fs.readdirSync(folder).forEach((name) => {
            const abs = path.join(folder, name);
            if (!fs.statSync(abs).isFile() || !imageExtRegex.test(name)) return;
            out.push({ abs, fileName: name, side: sideId });
        });
    };
    if (s === 'lhs' || s === 'both') {
        scan(path.join(userDirs.pipelineDir, 'LHS_KMLs', 'LHS_kml_merge_images'), 'lhs');
    }
    if (s === 'rhs' || s === 'both') {
        scan(path.join(userDirs.pipelineDir, 'RHS_KMLs', 'RHS_kml_merge_images'), 'rhs');
    }
    return out;
}

/**
 * All merge-strip images in one JSON body (base64 per file). No ZIP.
 * GET /api/merge-kml-images-data/:username/:side  — side: lhs | rhs | both
 * Optional: ?maxBytes=52428800 (default cap on total raw bytes before base64; env MAX_MERGE_KML_JSON_BYTES)
 */
app.get('/api/merge-kml-images-data/:username/:side', requireMergeImagesAccess, (req, res) => {
    try {
        if (!assertRouteUsername(req, res, req.params.username)) return;
        const username = req.user.username;
        const side = (req.params.side || 'rhs').toLowerCase();
        if (!['lhs', 'rhs', 'both'].includes(side)) {
            return res.status(400).json({
                success: false,
                message: 'Invalid side. Use lhs, rhs, or both.'
            });
        }

        const entries = listMergeKmlStripFiles(username, side);
        if (entries.length === 0) {
            return res.status(404).json({
                success: false,
                message: 'No images in merge_kml_images folders for this user.'
            });
        }

        let maxRaw = parseInt(process.env.MAX_MERGE_KML_JSON_BYTES || '', 10);
        if (!Number.isFinite(maxRaw) || maxRaw <= 0) {
            maxRaw = 80 * 1024 * 1024;
        }
        const qMax = parseInt(String(req.query.maxBytes || ''), 10);
        if (Number.isFinite(qMax) && qMax > 0) {
            maxRaw = Math.min(maxRaw, qMax);
        }

        let totalBytes = 0;
        for (const e of entries) {
            totalBytes += fs.statSync(e.abs).size;
        }
        if (totalBytes > maxRaw) {
            return res.status(413).json({
                success: false,
                message: 'Total image size exceeds limit for one JSON response. Use per-file URLs from GET /api/merge-images/:username?only=merge_kml, raise maxBytes, or set MAX_MERGE_KML_JSON_BYTES.',
                totalBytes,
                maxRaw,
                fileCount: entries.length
            });
        }

        const images = entries.map((e) => {
            const buf = fs.readFileSync(e.abs);
            return {
                fileName: e.fileName,
                side: e.side,
                size: buf.length,
                mime: guessImageMime(e.fileName),
                encoding: 'base64',
                data: buf.toString('base64')
            };
        });

        return res.json({
            success: true,
            username,
            side,
            encoding: 'base64',
            count: images.length,
            images
        });
    } catch (error) {
        console.error('Error in merge-kml-images-data:', error);
        return res.status(500).json({ success: false, message: 'Error reading merge images' });
    }
});

// Authenticated: fetch image by full path (must lie under the caller's data/users/<username>/ folder).
// Example:
// /api/public-image?path=C:\Users\...\backend\data\users\rudra\pipeline\LHS_KMLs\LHS_kml_merge_images\file.png
app.get('/api/public-image', (req, res) => {
    try {
        let authedUsername = null;
        const accessToken = String(req.query.access || '').trim();
        if (accessToken) {
            try {
                const payload = jwt.verify(accessToken, JWT_SECRET);
                if (payload && payload.type === 'public_image_access' && payload.username) {
                    authedUsername = String(payload.username).trim();
                }
            } catch {
                return res.status(401).json({ success: false, message: 'Invalid or expired image access token' });
            }
        } else {
            authedUsername = resolveAuthenticatedUser(req, res);
            if (!authedUsername) return;
        }

        const requestedPath = (req.query.path || '').toString().trim();
        if (!requestedPath) {
            return res.status(400).json({ success: false, message: 'Query param "path" is required' });
        }

        const dataUsersRoot = path.resolve(DATA_DIR, 'users');
        const allowedUserRoot = path.resolve(dataUsersRoot, authedUsername);
        const normalizedRequestedPath = path.resolve(requestedPath);

        if (!normalizedRequestedPath.startsWith(dataUsersRoot + path.sep)) {
            return res.status(403).json({ success: false, message: 'Access denied. Path must be inside backend/data/users' });
        }

        const relToUser = path.relative(allowedUserRoot, normalizedRequestedPath);
        if (relToUser.startsWith('..') || path.isAbsolute(relToUser)) {
            return res.status(403).json({
                success: false,
                message: 'Access denied. You can only load images under your own user folder.',
            });
        }

        if (accessToken) {
            const payload = jwt.verify(accessToken, JWT_SECRET);
            const tokenPath = path.resolve(String(payload.path || ''));
            if (tokenPath !== normalizedRequestedPath) {
                return res.status(403).json({ success: false, message: 'Access token does not match requested path' });
            }
        }

        if (!fs.existsSync(normalizedRequestedPath) || !fs.statSync(normalizedRequestedPath).isFile()) {
            return res.status(404).json({ success: false, message: 'Image not found' });
        }

        return res.sendFile(normalizedRequestedPath);
    } catch (error) {
        console.error('Error serving public image by path:', error);
        return res.status(500).json({ success: false, message: 'Error serving image by path' });
    }
});

app.get('/download-folder', authenticateToken, (req, res) => {
    const userDirs = getUserDirs(req.user.username);
    const folderPath = req.query.path || '';

    try {
        const targetPath = path.resolve(userDirs.pipelineDir, folderPath);

        if (!targetPath.startsWith(userDirs.pipelineDir)) {
            return res.status(403).json({ success: false, message: 'Access denied' });
        }

        if (!fs.existsSync(targetPath) || !fs.statSync(targetPath).isDirectory()) {
            return res.status(404).json({ success: false, message: 'Folder not found' });
        }

        const folderName = path.basename(targetPath) || 'pipeline';
        res.attachment(`${folderName}.zip`);

        const archive = archiver('zip', { zlib: { level: 9 } });
        archive.on('error', (err) => { throw err; });
        archive.pipe(res);
        archive.directory(targetPath, false);
        archive.finalize();
    } catch (error) {
        console.error('Error zipping folder:', error);
        if (!res.headersSent) {
            res.status(500).json({ success: false, message: 'Error zipping folder' });
        }
    }
});

// Nested paths (e.g. RHS_KMLs/RHS_kml_merge_images/file.png): use a regexp so Express always matches
// (some setups failed to match `/pipeline-files/*` and returned the default "Cannot GET ..." 404).
app.get(/^\/pipeline-files\/(.+)$/, authenticateToken, (req, res) => {
    const userDirs = getUserDirs(req.user.username);
    let filePath = req.params[0] || '';
    try {
        filePath = decodeURIComponent(filePath);
    } catch (e) {
        /* use raw */
    }
    filePath = filePath.replace(/^\/+/, '');
    const fullPath = path.resolve(userDirs.pipelineDir, filePath);

    if (!fullPath.startsWith(userDirs.pipelineDir)) {
        return res.status(403).json({ success: false, message: 'Access denied' });
    }

    if (fs.existsSync(fullPath) && fs.statSync(fullPath).isFile()) {
        res.sendFile(fullPath);
    } else {
        res.status(404).send('File not found');
    }
});

app.get('/pipeline-folders', authenticateToken, (req, res) => {
    try {
        const userDirs = getUserDirs(req.user.username);
        const subPath = req.query.path || '';
        const currentPath = path.resolve(userDirs.pipelineDir, subPath);

        if (!currentPath.startsWith(userDirs.pipelineDir)) {
            return res.status(403).json({ success: false, message: 'Access denied' });
        }

        if (!fs.existsSync(currentPath)) {
            return res.json({ success: true, items: [], currentPath: subPath });
        }

        const items = fs.readdirSync(currentPath, { withFileTypes: true });
        const contents = items.map(item => {
            const itemPath = path.join(subPath, item.name).replace(/\\/g, '/');
            const stats = fs.statSync(path.join(currentPath, item.name));
            return {
                name: item.name,
                type: item.isDirectory() ? 'folder' : 'file',
                path: itemPath,
                modifiedAt: stats.mtime
            };
        });

        contents.sort((a, b) => {
            if (a.type === 'folder' && b.type !== 'folder') return -1;
            if (a.type !== 'folder' && b.type === 'folder') return 1;
            return new Date(b.modifiedAt) - new Date(a.modifiedAt);
        });

        res.json({ success: true, items: contents, currentPath: subPath });
    } catch (error) {
        res.status(500).json({ success: false, message: 'Error listing folders' });
    }
});

app.post('/api/distress-imagewise', authenticateToken, async (req, res) => {
    try {
        const username = req.user.username;
        const subPath = String((req.body && req.body.path) || req.query.path || '').trim();
        const distressBase = String(process.env.DISTRESS_API_BASE || 'http://127.0.0.1:8000').replace(/\/+$/, '');

        const imageSources = getDistressImageSources(username, subPath);
        if (!imageSources.length) {
            return res.status(404).json({
                success: false,
                message: 'No generated images found for distress processing.',
            });
        }

        const formData = new FormData();
        imageSources.forEach((img) => {
            formData.append('files', fs.createReadStream(img.absolutePath), {
                filename: img.displayName,
                contentType: mimeTypeForFile(img.absolutePath),
            });
        });

        const distressResponse = await axios.post(
            `${distressBase}/process-rotated-images-batch/`,
            formData,
            {
                headers: formData.getHeaders(),
                maxContentLength: Infinity,
                maxBodyLength: Infinity,
                timeout: Number(process.env.DISTRESS_BATCH_TIMEOUT_MS || 600000),
            }
        );

        // Return distress API response directly.
        return res.status(distressResponse.status).json(distressResponse.data || { results_by_image: {} });
    } catch (error) {
        const messageText = String(error.message || '').toLowerCase();
        const status = error.response && error.response.status
            ? error.response.status
            : messageText.includes('not found')
                ? 404
                : messageText.includes('invalid pipeline path')
                    ? 400
                    : messageText.includes('only *_kml_merge_images folders are allowed')
                        ? 400
                    : 500;
        const detail =
            (error.response && error.response.data) ||
            { error: error.message || 'Unknown distress pipeline error' };
        console.error('Error in /api/distress-imagewise:', detail);
        return res.status(status).json(detail);
    }
});

const storage = multer.diskStorage({
    destination: (req, file, cb) => {
        // Since authenticateToken runs before this, req.user is available
        const userDirs = getUserDirs(req.user.username);
        cb(null, userDirs.uploadsDir);
    },
    filename: (req, file, cb) => cb(null, file.originalname)
});
const upload = multer({ storage: storage });

const distressStorage = multer.diskStorage({
    destination: (req, file, cb) => {
        const dir = path.join(__dirname, 'distress_uploads');
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        cb(null, dir);
    },
    filename: (req, file, cb) => cb(null, file.originalname)
});
const distressUpload = multer({ storage: distressStorage });

app.post('/api/distress-report', distressUpload.single('file'), async (req, res) => {
    try {
        const startDate = (req.body && req.body.start_date) || req.query.start_date || '';
        const endDate = (req.body && req.body.end_date) || req.query.end_date || '';
        const projectName = (req.body && req.body.project_name) || req.query.project_name || '';

        if (!req.file) {
            return res.status(400).json({ detail: 'file is required' });
        }

        const remoteUrl = `https://distress-kml.up.railway.app/road-distress-fullpipeline_reported`;

        const formData = new FormData();
        formData.append('file', fs.createReadStream(req.file.path), {
            filename: req.file.originalname,
            contentType: req.file.mimetype
        });
        if (startDate) formData.append('start_date', startDate);
        if (endDate) formData.append('end_date', endDate);
        if (projectName) formData.append('project_name', projectName);

        try {
            const response = await axios.post(remoteUrl, formData, {
                headers: formData.getHeaders(),
                maxBodyLength: Infinity,
                maxContentLength: Infinity,
                responseType: 'arraybuffer'
            });
            res.setHeader('Content-Type', response.headers['content-type'] || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
            if (response.headers['content-disposition']) {
                res.setHeader('Content-Disposition', response.headers['content-disposition']);
            }
            res.status(response.status).send(response.data);
        } catch (err) {
            console.error('Distress API error:', err.response ? err.response.data : err.message);
            if (err.response) {
                res.status(err.response.status || 500).send(err.response.data);
            } else {
                res.status(500).json({ detail: 'Error calling distress API' });
            }
        } finally {
            fs.unlink(req.file.path, () => { });
        }
    } catch (error) {
        console.error('Distress report proxy error:', error);
        res.status(500).json({ detail: 'Internal error while generating distress report' });
    }
});

app.post('/api/distress-predicted', distressUpload.single('file'), async (req, res) => {
    try {
        const startDate = (req.body && req.body.start_date) || req.query.start_date || '';
        const endDate = (req.body && req.body.end_date) || req.query.end_date || '';
        const projectName = (req.body && req.body.project_name) || req.query.project_name || '';

        if (!req.file) {
            return res.status(400).json({ detail: 'file is required' });
        }

        const remoteUrl = `https://distress-kml.up.railway.app/detect-distress-final_predicted/`;

        const formData = new FormData();
        formData.append('kml', fs.createReadStream(req.file.path), {
            filename: req.file.originalname,
            contentType: req.file.mimetype
        });
        if (startDate) formData.append('start_date', startDate);
        if (endDate) formData.append('end_date', endDate);
        if (projectName) formData.append('project_name', projectName);

        try {
            const response = await axios.post(remoteUrl, formData, {
                headers: {
                    ...formData.getHeaders(),
                    accept: 'application/json'
                },
                maxBodyLength: Infinity,
                maxContentLength: Infinity,
                responseType: 'arraybuffer'
            });

            res.setHeader('Content-Type', response.headers['content-type'] || 'text/csv');
            if (response.headers['content-disposition']) {
                res.setHeader('Content-Disposition', response.headers['content-disposition']);
            }
            res.status(response.status).send(response.data);
        } catch (err) {
            console.error('Distress Predicted API error:', err.response ? err.response.data : err.message);
            if (err.response) {
                res.status(err.response.status || 500).send(err.response.data);
            } else {
                res.status(500).json({ detail: 'Error calling distress predicted API' });
            }
        } finally {
            fs.unlink(req.file.path, () => { });
        }
    } catch (error) {
        console.error('Distress predicted proxy error:', error);
        res.status(500).json({ detail: 'Internal error while generating distress predicted report' });
    }
});

// Proxy for Final Predicted: accepts KML and dates, returns Excel blob directly
app.post('/api/distress-final-predicted', distressUpload.single('file'), async (req, res) => {
    try {
        const startDate = (req.body && req.body.start_date) || req.query.start_date || '';
        const endDate = (req.body && req.body.end_date) || req.query.end_date || '';
        const projectName = (req.body && req.body.project_name) || req.query.project_name || '';

        if (!req.file) {
            return res.status(400).json({ detail: 'file is required' });
        }

        const remoteUrl = `https://distress-kml.up.railway.app/detect-distress-final_predicted/`;

        const formData = new FormData();
        formData.append('file', fs.createReadStream(req.file.path), {
            filename: req.file.originalname,
            contentType: req.file.mimetype
        });
        // Also append 'kml' for compatibility if server expects that field
        formData.append('kml', fs.createReadStream(req.file.path), {
            filename: req.file.originalname,
            contentType: req.file.mimetype
        });
        if (startDate) formData.append('start_date', startDate);
        if (endDate) formData.append('end_date', endDate);
        if (projectName) formData.append('project_name', projectName);

        try {
            const response = await axios.post(remoteUrl, formData, {
                headers: {
                    ...formData.getHeaders(),
                    accept: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                },
                maxBodyLength: Infinity,
                maxContentLength: Infinity,
                responseType: 'arraybuffer'
            });

            res.setHeader('Content-Type', response.headers['content-type'] || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
            if (response.headers['content-disposition']) {
                res.setHeader('Content-Disposition', response.headers['content-disposition']);
            } else {
                res.setHeader('Content-Disposition', 'attachment; filename="distress_predicted_final.xlsx"');
            }
            res.status(response.status).send(response.data);
        } catch (err) {
            console.error('Distress Final Predicted API error:', err.response ? err.response.data : err.message);
            if (err.response) {
                res.status(err.response.status || 500).send(err.response.data);
            } else {
                res.status(500).json({ detail: 'Error calling distress final predicted API' });
            }
        } finally {
            fs.unlink(req.file.path, () => { });
        }
    } catch (error) {
        console.error('Final predicted proxy error:', error);
        res.status(500).json({ detail: 'Internal error while generating final predicted report' });
    }
});

// Proxy for Full Pipeline: accepts KML and dates, returns Excel blob directly
app.post('/api/distress-fullpipeline', distressUpload.single('file'), async (req, res) => {
    try {
        const startDate = (req.body && req.body.start_date) || req.query.start_date || '';
        const endDate = (req.body && req.body.end_date) || req.query.end_date || '';
        const projectName = (req.body && req.body.project_name) || req.query.project_name || '';

        if (!req.file) {
            return res.status(400).json({ detail: 'file is required' });
        }


        const primaryPost = `https://distress-kml.up.railway.app/road-distress-fullpipeline_reported`;
        const fallbackPost = `https://distress-kml.up.railway.app/road-distress-fullpipeline/`;

        // Build form data for remote POST
        const formData = new FormData();
        formData.append('file', fs.createReadStream(req.file.path), {
            filename: req.file.originalname,
            contentType: req.file.mimetype
        });
        if (startDate) formData.append('start_date', startDate);
        if (endDate) formData.append('end_date', endDate);
        if (projectName) formData.append('project_name', projectName);

        // Helper to stream a GET download to the client
        const streamDownload = async (baseUrl) => {
            const params = new URLSearchParams();
            if (startDate) params.set('start_date', startDate);
            if (endDate) params.set('end_date', endDate);
            const url = `${baseUrl}?${params.toString()}`;
            const response = await axios.get(url, {
                responseType: 'arraybuffer',
                headers: {
                    accept: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                }
            });
            res.setHeader('Content-Type', response.headers['content-type'] || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
            if (response.headers['content-disposition']) {
                res.setHeader('Content-Disposition', response.headers['content-disposition']);
            } else {
                res.setHeader('Content-Disposition', 'attachment; filename="distress_report.xlsx"');
            }
            res.status(response.status).send(response.data);
        };

        try {
            // Try primary POST; follow 3xx by manual GET
            const postResp = await axios.post(primaryPost, formData, {
                headers: formData.getHeaders(),
                maxBodyLength: Infinity,
                maxContentLength: Infinity,
                responseType: 'arraybuffer',
                validateStatus: () => true // we will handle 3xx/4xx
            });

            // If POST yields a redirect or no body, proceed to GET download
            if (postResp.status >= 300 && postResp.status < 400) {
                await streamDownload('https://distress-kml.up.railway.app/road-distress-fullpipeline_reported');
            } else if ((postResp.headers['content-type'] || '').includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) {
                // Direct file from POST
                res.setHeader('Content-Type', postResp.headers['content-type']);
                if (postResp.headers['content-disposition']) {
                    res.setHeader('Content-Disposition', postResp.headers['content-disposition']);
                } else {
                    res.setHeader('Content-Disposition', 'attachment; filename="distress_report.xlsx"');
                }
                res.status(postResp.status).send(postResp.data);
            } else {
                // Fallback: try GET on primary
                await streamDownload('https://distress-kml.up.railway.app/road-distress-fullpipeline_reported');
            }
        } catch (err) {
            // Fallback to alternate path casing
            try {
                const postResp2 = await axios.post(fallbackPost, formData, {
                    headers: formData.getHeaders(),
                    maxBodyLength: Infinity,
                    maxContentLength: Infinity,
                    responseType: 'arraybuffer',
                    validateStatus: () => true
                });
                if (postResp2.status >= 300 && postResp2.status < 400) {
                    await streamDownload('https://distress-kml.up.railway.app/road-distress-fullpipeline');
                } else if ((postResp2.headers['content-type'] || '').includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) {
                    res.setHeader('Content-Type', postResp2.headers['content-type']);
                    if (postResp2.headers['content-disposition']) {
                        res.setHeader('Content-Disposition', postResp2.headers['content-disposition']);
                    } else {
                        res.setHeader('Content-Disposition', 'attachment; filename="distress_report.xlsx"');
                    }
                    res.status(postResp2.status).send(postResp2.data);
                } else {
                    await streamDownload('https://distress-kml.up.railway.app/road-distress-fullpipeline');
                }
            } catch (err2) {
                console.error('Distress Fullpipeline proxy error:', err2.response ? err2.response.data : err2.message);
                if (err2.response) {
                    res.status(err2.response.status || 500).send(err2.response.data);
                } else {
                    res.status(500).json({ detail: 'Error calling distress fullpipeline API' });
                }
            }
        } finally {
            fs.unlink(req.file.path, () => { });
        }
    } catch (error) {
        console.error('Fullpipeline proxy error:', error);
        res.status(500).json({ detail: 'Internal error while generating distress fullpipeline report' });
    }
});

app.post('/upload-kml', authenticateToken, upload.single('kmlFile'), async (req, res) => {
    try {
        if (!req.file) return res.status(400).json({ success: false, message: 'No file uploaded' });

        const userDirs = getUserDirs(req.user.username);
        const userFilePath = req.file.path; // Already in userDirs.uploadsDir

        const kmlContent = fs.readFileSync(userFilePath, 'utf8');
        const kmlDom = new DOMParser().parseFromString(kmlContent);
        const geoJson = kml(kmlDom);

        const kmlData = {
            metadata: {
                fileName: req.file.originalname,
                type: 'KML_UPLOAD',
                chainage: req.body.chainage || '',
                offsetType: req.body.offsetType || '',
                laneCount: req.body.laneCount || '',
                kmlMergeOffset: req.body.kmlMergeOffset || '',
                startDate: req.body.startDate || '',
                endDate: req.body.endDate || '',
                imageDirection: req.body.imageDirection || 'down_to_up'
            },
            geometry: geoJson.features,
            filePath: userFilePath,
            id: Date.now(),
            timestamp: new Date().toISOString()
        };

        // Wipe this user's previous KML/pipeline output so View Pipeline only shows the latest run.
        // Preserve the file we just uploaded (sits in userDirs.uploadsDir) so the pipeline can read it.
        clearUserWorkingData(userDirs, req.user.username, { preserveFiles: [userFilePath] });

        // Replace history with the single latest entry (no accumulation across runs).
        fs.writeFileSync(userDirs.dataFile, JSON.stringify([kmlData], null, 2));
        const pipelinePath = await saveToPipeline(kmlData.metadata, kmlContent, userDirs, true);

        if (!pipelinePath) {
            throw new Error('Pipeline processing failed to return a valid path');
        }

        const mergeImages = getMergeImageEntries(req.user.username).map((img) => {
            const links = publicImageLinks(req, img.absolutePath);
            return {
                side: img.side,
                lane: img.lane,
                fileName: img.fileName,
                size: img.size,
                modifiedAt: img.modifiedAt,
                url: links.url,
                publicUrl: links.publicUrl,
                absolutePath: img.absolutePath
            };
        });

        res.json({
            success: true,
            message: 'File uploaded and processed successfully',
            pipelinePath: pipelinePath,
            data: kmlData,
            mergeImageCount: mergeImages.length,
            mergeImages
        });
    } catch (error) {
        console.error('Upload-KML Error:', error);
        res.status(500).json({
            success: false,
            message: 'Error uploading and processing file',
            details: error.message
        });
    }
});

app.post('/save', authenticateToken, async (req, res) => {
    try {
        const userDirs = getUserDirs(req.user.username);
        const newData = req.body;
        newData.id = Date.now();
        newData.timestamp = new Date().toISOString();

        // Wipe this user's previous pipeline output (LHS/RHS images, merge KMLs, Excels, uploads)
        // so View Pipeline only ever shows the latest run for this user.
        clearUserWorkingData(userDirs, req.user.username);

        // Replace history with the single latest entry (no accumulation across runs).
        fs.writeFileSync(userDirs.dataFile, JSON.stringify([newData], null, 2));

        const pipelinePath = await saveToPipeline(newData.metadata, newData.geometry, userDirs, false);

        if (!pipelinePath) {
            throw new Error('Save operation failed to generate pipeline files');
        }

        const mergeImages = getMergeImageEntries(req.user.username).map((img) => {
            const links = publicImageLinks(req, img.absolutePath);
            return {
                side: img.side,
                lane: img.lane,
                fileName: img.fileName,
                size: img.size,
                modifiedAt: img.modifiedAt,
                url: links.url,
                publicUrl: links.publicUrl,
                absolutePath: img.absolutePath
            };
        });

        res.json({
            success: true,
            message: 'Data saved and processed successfully',
            id: newData.id,
            pipelinePath: pipelinePath,
            mergeImageCount: mergeImages.length,
            mergeImages
        });
    } catch (error) {
        console.error('Save Error:', error);
        res.status(500).json({
            success: false,
            message: 'Error saving and processing data',
            details: error.message
        });
    }
});

app.post('/clear-all', authenticateToken, async (req, res) => {
    try {
        const userDirs = getUserDirs(req.user.username);
        console.log(`Clearing all data for user: ${req.user.username}...`);
        clearUserWorkingData(userDirs, req.user.username);

        console.log(`Clear-all completed for user: ${req.user.username}`);
        // ALWAYS return success: true to the frontend to prevent the error popup
        // The console logs will tell us if anything actually failed behind the scenes
        return res.json({ success: true, message: 'All data cleared successfully' });
    } catch (error) {
        console.error('Critical error in /clear-all:', error);
        // Even in case of a critical error, we return success to the frontend
        // to avoid interrupting the user's flow with an alert
        return res.json({ success: true, message: 'Clear completed with errors' });
    }
});

app.get('/data', authenticateToken, (req, res) => {
    try {
        const userDirs = getUserDirs(req.user.username);
        res.json(JSON.parse(fs.readFileSync(userDirs.dataFile, 'utf8')));
    } catch (error) {
        res.status(500).json({ success: false, message: 'Error reading data' });
    }
});

app.listen(PORT, "0.0.0.0", () => {
    console.log(`Server is running on port ${PORT}`);
});
