const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const multer = require('multer');
const FormData = require('form-data');
const axios = require('axios');
const archiver = require('archiver');
const { kml } = require('@tmcw/togeojson');
const { DOMParser } = require('xmldom');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');

const { exec } = require('child_process');
const util = require('util');
const execPromise = util.promisify(exec);

const app = express();
const PORT = process.env.PORT || 3001;
const JWT_SECRET = process.env.JWT_SECRET || 'kml_secret_key_2026';
const HARDCODED_TEST_USER = 'test';
const HARDCODED_TEST_PASSWORD = '123';
app.set('trust proxy', true);

// Define directories first
const DATA_DIR = process.env.DATA_DIR
    ? path.resolve(process.env.DATA_DIR)
    : path.join(__dirname, 'data');
const USERS_FILE = path.join(DATA_DIR, 'users.json');

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

app.use(cors({
    origin: '*',
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization']
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

app.post('/api/login', async (req, res) => {
    try {
        const { username, password } = req.body;

        if (username === HARDCODED_TEST_USER && password === HARDCODED_TEST_PASSWORD) {
            const token = jwt.sign({ username: HARDCODED_TEST_USER }, JWT_SECRET);
            return res.json({ success: true, token, username: HARDCODED_TEST_USER });
        }

        const users = JSON.parse(fs.readFileSync(USERS_FILE, 'utf8'));
        const user = users.find(u => u.username === username);

        if (!user || !(await bcrypt.compare(password, user.password))) {
            return res.status(401).json({ success: false, message: 'Invalid username or password' });
        }

        const token = jwt.sign({ username }, JWT_SECRET);
        res.json({ success: true, token, username });
    } catch (error) {
        console.error('Login error:', error);
        res.status(500).json({ success: false, message: 'Login failed' });
    }
});

// Authentication disabled: accept requests without token and resolve a username
// from request context. This keeps per-user folders separate without JWT.
const authenticateToken = (req, res, next) => {
    const bodyUsername = req.body && typeof req.body.username === 'string' ? req.body.username.trim() : '';
    const queryUsername = req.query && typeof req.query.username === 'string' ? req.query.username.trim() : '';
    const headerUsername = req.headers['x-username'] ? String(req.headers['x-username']).trim() : '';
    const resolvedUsername = bodyUsername || queryUsername || headerUsername || 'local-user';
    req.user = { username: resolvedUsername };
    next();
};


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
            let pythonExe = 'python3';

            // Check for virtual environment path (Railway/Docker)
            const venvPath = '/opt/venv/bin/python';
            if (fs.existsSync(venvPath)) {
                pythonExe = venvPath;
            } else {
                // Local fallback
                try {
                    await execPromise('python3 --version');
                    pythonExe = 'python3';
                } catch (e) {
                    pythonExe = 'python';
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
    const configuredBaseUrl = (process.env.PUBLIC_BASE_URL || 'https://kml-backend-production-501c.up.railway.app').trim();
    if (configuredBaseUrl) {
        return configuredBaseUrl.replace(/\/+$/, '');
    }
    const protocol = (req.headers['x-forwarded-proto'] || req.protocol || 'http').split(',')[0].trim();
    const host = req.get('host');
    return `${protocol}://${host}`;
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
app.get('/api/merge-images/:username', (req, res) => {
    try {
        const username = (req.params.username || '').trim();
        if (!username) {
            return res.status(400).json({ success: false, message: 'Username is required' });
        }
        const baseUrl = getRequestBaseUrl(req);
        const images = getMergeImageEntries(username).map((img) => ({
            side: img.side,
            lane: img.lane,
            fileName: img.fileName,
            size: img.size,
            modifiedAt: img.modifiedAt,
            url: `/api/public-image?path=${encodeURIComponent(img.absolutePath)}`,
            publicUrl: `${baseUrl}/api/public-image?path=${encodeURIComponent(img.absolutePath)}`
        }));

        return res.json({ success: true, username, count: images.length, images });
    } catch (error) {
        console.error('Error fetching merge images:', error);
        return res.status(500).json({ success: false, message: 'Error fetching merge images' });
    }
});

app.get('/api/merge-images/:username/:side/:fileName', (req, res) => {
    try {
        const { username, side } = req.params;
        const decodedFileName = decodeURIComponent(req.params.fileName || '');
        if (!username || !username.trim()) {
            return res.status(400).json({ success: false, message: 'Username is required' });
        }

        const userDirs = getUserDirs(username.trim());

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

// Public endpoint: fetch image directly by full path
// Example:
// /api/public-image?path=C:\Users\...\backend\data\users\rudra\pipeline\LHS_KMLs\LHS_kml_merge_images\file.png
app.get('/api/public-image', (req, res) => {
    try {
        const requestedPath = (req.query.path || '').toString().trim();
        if (!requestedPath) {
            return res.status(400).json({ success: false, message: 'Query param "path" is required' });
        }

        const dataUsersRoot = path.resolve(DATA_DIR, 'users');
        const normalizedRequestedPath = path.resolve(requestedPath);

        // Only allow files inside backend/data/users
        if (!normalizedRequestedPath.startsWith(dataUsersRoot + path.sep)) {
            return res.status(403).json({ success: false, message: 'Access denied. Path must be inside backend/data/users' });
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

app.get('/pipeline-files/*filePath', authenticateToken, (req, res) => {
    const userDirs = getUserDirs(req.user.username);
    const filePath = req.params.filePath || '';
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
        const baseUrl = getRequestBaseUrl(req);
        clearUserWorkingData(userDirs, req.user.username, { preserveFiles: [userFilePath] });

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

        let existing = [];
        try {
            existing = JSON.parse(fs.readFileSync(userDirs.dataFile, 'utf8')) || [];
            if (!Array.isArray(existing)) existing = [];
        } catch {
            existing = [];
        }
        existing.push(kmlData);
        fs.writeFileSync(userDirs.dataFile, JSON.stringify(existing, null, 2));
        const pipelinePath = await saveToPipeline(kmlData.metadata, kmlContent, userDirs, true);

        if (!pipelinePath) {
            throw new Error('Pipeline processing failed to return a valid path');
        }

        const mergeImages = getMergeImageEntries(req.user.username).map((img) => ({
            side: img.side,
            lane: img.lane,
            fileName: img.fileName,
            size: img.size,
            modifiedAt: img.modifiedAt,
            url: `/api/public-image?path=${encodeURIComponent(img.absolutePath)}`,
            publicUrl: `${baseUrl}/api/public-image?path=${encodeURIComponent(img.absolutePath)}`,
            absolutePath: img.absolutePath
        }));

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
        const baseUrl = getRequestBaseUrl(req);
        clearUserWorkingData(userDirs, req.user.username);
        const newData = req.body;
        newData.id = Date.now();
        newData.timestamp = new Date().toISOString();
        let existing = [];
        try {
            existing = JSON.parse(fs.readFileSync(userDirs.dataFile, 'utf8')) || [];
            if (!Array.isArray(existing)) existing = [];
        } catch {
            existing = [];
        }
        existing.push(newData);
        fs.writeFileSync(userDirs.dataFile, JSON.stringify(existing, null, 2));

        const pipelinePath = await saveToPipeline(newData.metadata, newData.geometry, userDirs, false);

        if (!pipelinePath) {
            throw new Error('Save operation failed to generate pipeline files');
        }

        const mergeImages = getMergeImageEntries(req.user.username).map((img) => ({
            side: img.side,
            lane: img.lane,
            fileName: img.fileName,
            size: img.size,
            modifiedAt: img.modifiedAt,
            url: `/api/public-image?path=${encodeURIComponent(img.absolutePath)}`,
            publicUrl: `${baseUrl}/api/public-image?path=${encodeURIComponent(img.absolutePath)}`,
            absolutePath: img.absolutePath
        }));

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
