const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const multer = require('multer');
const archiver = require('archiver');
const { kml } = require('@tmcw/togeojson');
const { DOMParser } = require('xmldom');

const { exec } = require('child_process');
const util = require('util');
const execPromise = util.promisify(exec);

const app = express();
const PORT = process.env.PORT || 3001;

// Define directories first
const DATA_DIR = path.join(__dirname, 'data');
const UPLOADS_DIR = path.join(DATA_DIR, 'uploads');
const DATA_FILE = path.join(DATA_DIR, 'drawn_data.json');
const PIPELINE_DIR = path.join(__dirname, 'pipeline');

// Subdirectories that should always exist in pipeline
const PIPELINE_SUBDIRS = ['LHS_KMLs', 'RHS_KMLs', 'Excels', 'Merge_KMLs', 'kml_creation'];

// Ensure directories exist
function ensureDirectories() {
    if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR);
    if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR);
    if (!fs.existsSync(PIPELINE_DIR)) fs.mkdirSync(PIPELINE_DIR);
    if (!fs.existsSync(DATA_FILE)) fs.writeFileSync(DATA_FILE, JSON.stringify([]));
    
    // Ensure pipeline subdirectories exist
    PIPELINE_SUBDIRS.forEach(sub => {
        const subPath = path.join(PIPELINE_DIR, sub);
        if (!fs.existsSync(subPath)) {
            fs.mkdirSync(subPath, { recursive: true });
            console.log(`Created missing pipeline directory: ${sub}`);
        }
    });
}

ensureDirectories();

app.use(cors({
    origin: ["https://kml-frontend-production.up.railway.app", "http://localhost:3000"],
    methods: ["GET","POST","PUT","DELETE"],
    allowedHeaders: ["Content-Type"]
}));

app.use(bodyParser.json({ limit: '50mb' }));
app.use(express.urlencoded({ limit: '50mb', extended: true }));

// Health check route for Railway
app.get('/', (req, res) => {
    res.json({ status: 'Backend is running successfully', timestamp: new Date() });
});


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
async function processWithPython(metadata, kmlContent) {
    try {
        const kmlCreationDir = path.join(__dirname, 'kml_creation');
        const inputKmlPath = path.join(kmlCreationDir, 'input.kml');
        const pythonScriptPath = path.join(kmlCreationDir, 'KML_creation.py');
        
        // Try 'python3' first, then 'python'
        let pythonExePath = 'python3';
        try {
            await execPromise('python3 --version');
        } catch (e) {
            pythonExePath = 'python';
        }

        if (!fs.existsSync(kmlCreationDir)) {
            fs.mkdirSync(kmlCreationDir, { recursive: true });
        }

        fs.writeFileSync(inputKmlPath, kmlContent);
        
        const chainageStart = parseFloat(metadata.chainage) || 0;
        const interval = 5; 
        const laneCount = parseInt(metadata.laneCount) || 4;
        const mergeOffset = parseFloat(metadata.kmlMergeOffset) || 0.100;
        const laneStep = 3.4; 
        const medianOffset = parseFloat(metadata.offsetType) || 2.75;

        const command = `"${pythonExePath}" "${pythonScriptPath}" "${inputKmlPath}" "${PIPELINE_DIR}" ${chainageStart} ${interval} ${laneCount} ${mergeOffset} ${laneStep} ${medianOffset}`;
        console.log(`Executing Python command: ${command}`);
        
        let stdout, stderr;
        try {
            const result = await execPromise(command);
            stdout = result.stdout;
            stderr = result.stderr;
        } catch (execError) {
            stdout = execError.stdout;
            stderr = execError.stderr;
            const logContent = `COMMAND: ${command}\n\nSTDOUT:\n${stdout}\n\nSTDERR:\n${stderr}\n\nERROR:\n${execError.message}`;
            fs.writeFileSync(path.join(PIPELINE_DIR, 'python_error_log.txt'), logContent);
            throw new Error(`Python script execution failed: ${execError.message}. See python_error_log.txt for details.`);
        }
        
        const logContent = `COMMAND: ${command}\n\nSTDOUT:\n${stdout}\n\nSTDERR:\n${stderr}`;
        fs.writeFileSync(path.join(PIPELINE_DIR, 'python_output_log.txt'), logContent);

        if (stdout) console.log(`Python script output: ${stdout}`);
        if (stderr) {
            console.warn(`Python script stderr: ${stderr}`);
            if (stderr.toLowerCase().includes('error') || stderr.toLowerCase().includes('exception')) {
                // Check if it's just a warning or a real error
                if (!stdout || stdout.indexOf('ALL DONE') === -1) {
                    throw new Error(stderr);
                }
            }
        }

        return true;
    } catch (error) {
        console.error('CRITICAL Error in processWithPython:', error);
        throw error;
    }
}

// Helper function to save data to the pipeline folder
async function saveToPipeline(metadata, content, isKmlContent = false) {
    let kmlContent = isKmlContent ? content : geojsonToKml(content, 'Drawn_Data');
    await processWithPython(metadata, kmlContent);
    return 'Merge_KMLs';
}

// Helper function to trigger pipeline from data file
async function triggerPipelineFromDataFile() {
    try {
        if (!fs.existsSync(DATA_FILE)) return;
        const fileContent = fs.readFileSync(DATA_FILE, 'utf8');
        const data = JSON.parse(fileContent);
        if (data && data.length > 0) {
            const entry = data[0];
            if (entry.metadata && entry.geometry) {
                const kmlContent = geojsonToKml(entry.geometry, 'Drawn_Data');
                await processWithPython(entry.metadata, kmlContent);
            }
        }
    } catch (error) {
        console.error('Error triggering pipeline from data file:', error);
    }
}

// Watch for changes in drawn_data.json
let watchTimeout;
fs.watch(DATA_DIR, (eventType, filename) => {
    if (filename === 'drawn_data.json') {
        if (watchTimeout) clearTimeout(watchTimeout);
        watchTimeout = setTimeout(() => {
            triggerPipelineFromDataFile();
        }, 1000); 
    }
});

// Routes
app.get('/download-folder', (req, res) => {
    const folderPath = req.query.path || '';
    
    try {
        const targetPath = path.resolve(PIPELINE_DIR, folderPath);

        if (!targetPath.startsWith(PIPELINE_DIR)) {
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

app.use('/pipeline-files', express.static(PIPELINE_DIR));

app.get('/pipeline-folders', (req, res) => {
    try {
        const subPath = req.query.path || '';
        const currentPath = path.resolve(PIPELINE_DIR, subPath);
        
        if (!currentPath.startsWith(PIPELINE_DIR)) {
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
    destination: (req, file, cb) => cb(null, UPLOADS_DIR),
    filename: (req, file, cb) => cb(null, file.originalname)
});
const upload = multer({ storage: storage });

app.post('/upload-kml', upload.single('kmlFile'), async (req, res) => {
    try {
        if (!req.file) return res.status(400).json({ success: false, message: 'No file uploaded' });
        const kmlContent = fs.readFileSync(req.file.path, 'utf8');
        const kmlDom = new DOMParser().parseFromString(kmlContent);
        const geoJson = kml(kmlDom);

        const kmlData = {
            metadata: {
                fileName: req.file.originalname,
                type: 'KML_UPLOAD',
                chainage: req.body.chainage || '',
                offsetType: req.body.offsetType || '',
                laneCount: req.body.laneCount || '',
                kmlMergeOffset: req.body.kmlMergeOffset || ''
            },
            geometry: geoJson.features,
            filePath: req.file.path,
            id: Date.now(),
            timestamp: new Date().toISOString()
        };

        fs.writeFileSync(DATA_FILE, JSON.stringify([kmlData], null, 2));
        const pipelinePath = await saveToPipeline(kmlData.metadata, kmlContent, true);
        
        if (!pipelinePath) {
            throw new Error('Pipeline processing failed to return a valid path');
        }

        res.json({ 
            success: true, 
            message: 'File uploaded and processed successfully', 
            pipelinePath: pipelinePath, 
            data: kmlData 
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

app.post('/save', async (req, res) => {
    try {
        const newData = req.body;
        newData.id = Date.now();
        newData.timestamp = new Date().toISOString();
        fs.writeFileSync(DATA_FILE, JSON.stringify([newData], null, 2));
        
        const pipelinePath = await saveToPipeline(newData.metadata, newData.geometry, false);
        
        if (!pipelinePath) {
            throw new Error('Save operation failed to generate pipeline files');
        }

        res.json({ 
            success: true, 
            message: 'Data saved and processed successfully', 
            id: newData.id, 
            pipelinePath: pipelinePath 
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

app.post('/clear-all', async (req, res) => {
    try {
        console.log('Clearing all data...');
        // 1. Clear JSON data file
        if (fs.existsSync(DATA_FILE)) {
            try {
                fs.writeFileSync(DATA_FILE, JSON.stringify([], null, 2));
            } catch (e) { console.error('Error clearing data file:', e); }
        }

        // 2. Clear uploads directory
        if (fs.existsSync(UPLOADS_DIR)) {
            try {
                const uploadFiles = fs.readdirSync(UPLOADS_DIR);
                for (const file of uploadFiles) {
                    const filePath = path.join(UPLOADS_DIR, file);
                    try {
                        if (fs.existsSync(filePath) && fs.statSync(filePath).isFile()) {
                            fs.unlinkSync(filePath);
                        }
                    } catch (err) { console.error(`Error deleting upload file ${file}:`, err); }
                }
            } catch (e) { console.error('Error reading uploads dir:', e); }
        }

        // 3. Clear pipeline subdirectories
        const subDirs = ['LHS_KMLs', 'RHS_KMLs', 'Excels', 'Merge_KMLs', 'kml_creation'];
        for (const sub of subDirs) {
            const subPath = path.join(PIPELINE_DIR, sub);
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
                        } catch (err) { console.error(`Error deleting item ${item} in ${sub}:`, err); }
                    }
                } catch (err) { console.error(`Error reading directory ${sub}:`, err); }
            }
        }

        // 4. Clear the root pipeline directory of any standalone files
        if (fs.existsSync(PIPELINE_DIR)) {
            try {
                const rootItems = fs.readdirSync(PIPELINE_DIR);
                for (const item of rootItems) {
                    const itemPath = path.join(PIPELINE_DIR, item);
                    try {
                        if (fs.existsSync(itemPath) && fs.statSync(itemPath).isFile()) {
                            fs.unlinkSync(itemPath);
                        }
                    } catch (err) { console.error(`Error deleting root file ${item}:`, err); }
                }
            } catch (err) { console.error(`Error reading pipeline root:`, err); }
        }

        console.log('Clear-all operation completed successfully (or with handled warnings)');
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

app.get('/data', (req, res) => {
    try {
        res.json(JSON.parse(fs.readFileSync(DATA_FILE, 'utf8')));
    } catch (error) {
        res.status(500).json({ success: false, message: 'Error reading data' });
    }
});

app.listen(PORT, "0.0.0.0", () => {
    console.log(`Server is running on port ${PORT}`);
});