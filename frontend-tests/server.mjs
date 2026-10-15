import {createReadStream, statSync} from 'node:fs';
import {createServer} from 'node:http';
import {extname, join, normalize, resolve} from 'node:path';

const repositoryRoot = resolve(import.meta.dirname, '..');
const types = {
    '.css': 'text/css; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.pmtiles': 'application/octet-stream',
};

createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    const requested = url.pathname === '/' ? '/docs/index.html' : url.pathname;
    const relative = normalize(requested).replace(/^[/\\]+/, '');
    const filePath = join(repositoryRoot, relative);
    if (!filePath.startsWith(repositoryRoot)) {
        response.writeHead(403).end();
        return;
    }
    let size;
    try {
        size = statSync(filePath).size;
    } catch (_) {
        response.writeHead(404).end();
        return;
    }
    const headers = {
        'Accept-Ranges': 'bytes',
        'Content-Type': types[extname(filePath)] || 'application/octet-stream',
        'Cache-Control': 'no-store',
    };
    const range = request.headers.range;
    if (range) {
        const match = /^bytes=(\d+)-(\d*)$/.exec(range);
        if (!match) {
            response.writeHead(416, {'Content-Range': `bytes */${size}`}).end();
            return;
        }
        const start = Number(match[1]);
        const end = match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
        response.writeHead(206, {
            ...headers,
            'Content-Length': end - start + 1,
            'Content-Range': `bytes ${start}-${end}/${size}`,
        });
        createReadStream(filePath, {start, end}).pipe(response);
        return;
    }
    response.writeHead(200, {...headers, 'Content-Length': size});
    createReadStream(filePath).pipe(response);
}).listen(4173, '127.0.0.1');
