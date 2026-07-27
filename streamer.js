const express = require('express');
const cors = require('cors');

const app = express();
app.use(cors());

const PORT = 8778;

let client = null;
let parseTorrent = null;

// Catch uncaught exceptions to prevent internal WebTorrent engine errors from crashing node
process.on('uncaughtException', (err) => {
  console.error('[Streamer Uncaught Exception]:', err.message);
});

(async () => {
  const WebTorrentModule = await import('webtorrent');
  const WebTorrent = WebTorrentModule.default || WebTorrentModule;
  const ParseTorrentModule = await import('parse-torrent');
  parseTorrent = ParseTorrentModule.default || ParseTorrentModule;

  client = new WebTorrent();
  console.log('[WebTorrent] Engine & parse-torrent initialized.');
})();

const FAST_TRACKERS = [
  'udp://tracker.opentrackr.org:1337/announce',
  'udp://open.stealth.si:80/announce',
  'udp://tracker.torrent.eu.org:451/announce',
  'udp://tracker.bittor.pw:1337/announce',
  'udp://public.popcorn-tracker.org:6969/announce',
  'udp://tracker.dler.org:6969/announce',
  'udp://exodus.desync.com:6969/announce',
  'http://tracker.openbittorrent.com:80/announce',
  'udp://open.demonii.com:1337/announce'
];

app.get('/health', (req, res) => {
  if (!client) return res.status(503).json({ status: 'initializing' });
  res.json({ status: 'ok', torrents: client.torrents.length });
});

app.get('/stream', async (req, res) => {
  if (!client) {
    return res.status(503).send('Torrent engine starting up, please try again in a moment...');
  }

  let cleanMagnet = req.query.magnet;
  if (!cleanMagnet) {
    return res.status(400).send('Magnet parameter is required.');
  }

  // Clean double-encoded magnet links (%26 -> &, %3D -> =)
  try {
    while (cleanMagnet.includes('%26') || cleanMagnet.includes('%3D') || cleanMagnet.includes('%3a') || cleanMagnet.includes('%3A')) {
      const decoded = decodeURIComponent(cleanMagnet);
      if (decoded === cleanMagnet) break;
      cleanMagnet = decoded;
    }
  } catch (err) {}

  // Append high-speed trackers directly to magnet URI string
  FAST_TRACKERS.forEach(tr => {
    if (!cleanMagnet.includes(tr)) {
      cleanMagnet += `&tr=${tr}`;
    }
  });

  // Extract infoHash for duplicate check
  let infoHash = null;
  if (parseTorrent) {
    try {
      const parsed = await parseTorrent(cleanMagnet);
      infoHash = parsed.infoHash;
    } catch (e) {}
  }

  const getOrCreateTorrent = (magnetLink, torrentHash) => {
    return new Promise((resolve, reject) => {
      let torrent = torrentHash ? client.get(torrentHash) : client.get(magnetLink);
      
      // Clean up broken/destroyed/empty torrents from cache
      if (torrent && (torrent.destroyed || !torrent.files || torrent.files.length === 0)) {
        try { torrent.destroy(); } catch (e) {}
        torrent = null;
      }

      if (!torrent) {
        try {
          torrent = client.add(magnetLink);
        } catch (err) {
          torrent = torrentHash ? client.get(torrentHash) : client.get(magnetLink);
        }
      }

      if (!torrent) {
        return reject(new Error('Could not add magnet link to client.'));
      }

      if (torrent.ready && torrent.files && torrent.files.length > 0) {
        return resolve(torrent);
      }

      const timeout = setTimeout(() => {
        cleanup();
        if (torrent && typeof torrent.destroy === 'function') try { torrent.destroy(); } catch (e) {}
        reject(new Error('Timeout searching for seeders (45s). Torrent may have 0 seeds.'));
      }, 45000);

      const onReady = () => {
        if (torrent.files && torrent.files.length > 0) {
          cleanup();
          console.log(`[Streamer] Torrent ready: "${torrent.name}" (${torrent.files.length} files)`);
          resolve(torrent);
        }
      };

      const onError = (err) => {
        cleanup();
        console.error('[Streamer] Torrent error:', err.message);
        if (torrent && typeof torrent.destroy === 'function') try { torrent.destroy(); } catch (e) {}
        reject(err);
      };

      const cleanup = () => {
        clearTimeout(timeout);
        if (typeof torrent.removeListener === 'function') {
          torrent.removeListener('ready', onReady);
          torrent.removeListener('metadata', onReady);
          torrent.removeListener('error', onError);
        } else if (typeof torrent.off === 'function') {
          torrent.off('ready', onReady);
          torrent.off('metadata', onReady);
          torrent.off('error', onError);
        }
      };

      if (typeof torrent.on === 'function') {
        torrent.on('ready', onReady);
        torrent.on('metadata', onReady);
        torrent.on('error', onError);
      } else {
        resolve(torrent);
      }
    });
  };

  try {
    const torrent = await getOrCreateTorrent(cleanMagnet, infoHash);

    if (!torrent || !torrent.files || !Array.isArray(torrent.files) || !torrent.files.length) {
      return res.status(500).send('Torrent metadata loaded, but no files were found inside.');
    }

    // Find largest video file (.mp4, .mkv, .avi, .webm, .m4v, .mov)
    const videoFiles = torrent.files.filter(f => 
      /\.(mp4|mkv|avi|webm|m4v|mov)$/i.test(f.name)
    );

    const targetFile = videoFiles.length 
      ? videoFiles.slice().sort((a, b) => b.length - a.length)[0]
      : torrent.files.slice().sort((a, b) => b.length - a.length)[0];

    if (!targetFile) {
      return res.status(404).send('No playable media file found in torrent.');
    }

    // Prioritize target video file downloading
    torrent.files.forEach(f => {
      if (f !== targetFile && typeof f.deselect === 'function') f.deselect();
    });
    if (typeof targetFile.select === 'function') targetFile.select();

    // Content-Type mapping
    let contentType = 'video/mp4';
    const name = targetFile.name.toLowerCase();
    if (name.endsWith('.mkv')) contentType = 'video/x-matroska';
    else if (name.endsWith('.webm')) contentType = 'video/webm';
    else if (name.endsWith('.avi')) contentType = 'video/x-msvideo';

    const fileSize = targetFile.length;
    const range = req.headers.range;

    console.log(`[Stream] Serving "${targetFile.name}" (${(fileSize / 1024 / 1024).toFixed(1)} MB) Range: ${range || 'none'}`);

    if (range) {
      const parts = range.replace(/bytes=/, '').split('-');
      const start = parseInt(parts[0], 10);
      const end = parts[1] ? parseInt(parts[1], 10) : fileSize - 1;
      const chunkSize = (end - start) + 1;

      res.writeHead(206, {
        'Content-Range': `bytes ${start}-${end}/${fileSize}`,
        'Accept-Ranges': 'bytes',
        'Content-Length': chunkSize,
        'Content-Type': contentType,
        'Access-Control-Allow-Origin': '*'
      });

      const stream = targetFile.createReadStream({ start, end });
      stream.on('error', () => {});
      res.on('close', () => { if (typeof stream.destroy === 'function') stream.destroy(); });
      stream.pipe(res);
    } else {
      res.writeHead(200, {
        'Content-Length': fileSize,
        'Content-Type': contentType,
        'Accept-Ranges': 'bytes',
        'Access-Control-Allow-Origin': '*'
      });

      const stream = targetFile.createReadStream();
      stream.on('error', () => {});
      res.on('close', () => { if (typeof stream.destroy === 'function') stream.destroy(); });
      stream.pipe(res);
    }
  } catch (err) {
    console.error('[Streamer Catch Error]:', err.message);
    if (!res.headersSent) {
      res.status(500).send(`Torrent Error: ${err.message}`);
    }
  }
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`[Torrent Streamer] Listening on http://127.0.0.1:${PORT}`);
});
