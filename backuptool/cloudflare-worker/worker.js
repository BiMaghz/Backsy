// Constant-time comparison: both values are hashed to equal length first, so neither
// the content nor the length of the expected token leaks through timing.
async function safeEqual(a, b) {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest('SHA-256', enc.encode(a)),
    crypto.subtle.digest('SHA-256', enc.encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    const maxFileSize = 25 * 1024 * 1024; // 25MB
    const allowedMethods = ['GET', 'POST'];
    const authToken = env.API_TOKEN;
    
    if (!allowedMethods.includes(request.method)) {
      return new Response('Method Not Allowed', { status: 405 });
    }

    try {
      if (request.method === 'POST' && pathname === '/upload') {
        // Fail closed: a worker without API_TOKEN must not accept anonymous uploads.
        if (!authToken) {
          return new Response('Server misconfigured: API_TOKEN is not set', { status: 500 });
        }
        if (!(await safeEqual(request.headers.get('Authorization') || '', `Bearer ${authToken}`))) {
          return new Response('Unauthorized', { status: 401 });
        }

        const contentLength = parseInt(request.headers.get('Content-Length') || '0');
        if (contentLength > maxFileSize) {
          return new Response(`File too large. Max size: ${maxFileSize/1024/1024}MB`, { 
            status: 413 
          });
        }

        const formData = await request.formData();
        const file = formData.get('file');
        
        if (!file || typeof file === 'string') {
          return new Response('Invalid file upload', { status: 400 });
        }

        if (file.type !== 'application/gzip' && !file.name.endsWith('.tar.gz')) {
          return new Response('Only .tar.gz files are allowed', { status: 400 });
        }

        const day = new Date().toLocaleDateString('en-US', { 
          weekday: 'long', 
          year: 'numeric',
          month: 'short',
          day: 'numeric',
          timeZone: 'UTC'
        });

        const key = crypto.randomUUID();
        const fileBuffer = await file.arrayBuffer();

        await env.BACKUP_KV.put(key, fileBuffer, {
          expirationTtl: 86400, // 1 Day
          metadata: {
            filename: file.name,
            uploadedAt: new Date().toISOString(),
            size: file.size,
            day: day
          }
        });

        return new Response(JSON.stringify({ 
          token: key, 
          filename: `${day}-backup.tar.gz`,
          size: file.size,
          expiresIn: '24 hours'
        }), { 
          headers: { 
            'Content-Type': 'application/json',
            'Cache-Control': 'no-store'
          } 
        });
      }

      if (request.method === 'GET' && pathname.startsWith('/download/')) {
        const token = pathname.split('/').pop();
        
        if (!token || !/^[0-9a-f-]{36}$/.test(token)) {
          return new Response('Invalid token', { status: 400 });
        }

        const kvValue = await env.BACKUP_KV.getWithMetadata(token, { type: 'arrayBuffer' });
        
        if (!kvValue.value) {
          return new Response('File not found or expired', { status: 404 });
        }

        const metadata = kvValue.metadata || {};
        const rawName = metadata.filename || `${metadata.day || 'backup'}.tar.gz`;
        const filename = rawName.replace(/[^A-Za-z0-9._-]/g, '_'); // keep the header parameter well-formed

        const headers = {
          'Content-Type': 'application/gzip',
          'Content-Disposition': `attachment; filename="${filename}"`,
          'Cache-Control': 'no-store'
        };
        if (metadata.size) headers['Content-Length'] = String(metadata.size);

        return new Response(kvValue.value, { headers });
      }

      return new Response('Not Found', { status: 404 });

    } catch (error) {
      console.error('Worker Error:', error);
      return new Response('Internal Server Error', { status: 500 });
    }
  }
}
